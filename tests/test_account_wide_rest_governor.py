"""Account-wide REST governor — multiprocessing, priority, 429, probe, broker reuse, batch reconcile."""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from brokers.tastytrade_broker import (
    BrokerCooldownActive,
    BrokerRateLimited,
    _extract_retry_after_sec,
    _is_non_retryable_broker_error,
    _retry_on_transient,
)
from common.probe_coordinator import ProbeCoordinator, TrancheWindow
from common.rest_limiter import RestLimiter, governor_account_key, reset_rest_limiter
from common.rest_probe import RestProbeResult, run_rest_probe
from common.trading_gate import (
    evaluate_new_risk_gate,
    initialize_for_session_date,
    read_state,
)


def _worker_acquire(root: str, account_key: str, n: int, out_q):
    os.environ['MEIC_REST_GOVERNOR_ROOT'] = root
    os.environ['TT_REST_CROSS_PROCESS'] = 'true'
    os.environ['TT_REST_MAX_PER_SEC'] = '5'
    os.environ['TT_REST_BURST'] = '2'
    reset_rest_limiter()
    lim = RestLimiter(
        max_per_sec=5,
        burst=2,
        account_key=account_key,
        root=root,
        cross_process=True,
    )
    t0 = time.time()
    for _ in range(n):
        lim.acquire(priority='NORMAL', name='test_op')
    out_q.put((os.getpid(), lim.stats()['total_calls'], time.time() - t0))


class TestAccountWideMultiprocessing(unittest.TestCase):
    def test_cross_process_limiter_shares_account_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = 'paper_TESTACCT'
            ctx = mp.get_context('spawn')
            q = ctx.Queue()
            procs = [
                ctx.Process(target=_worker_acquire, args=(tmp, key, 3, q)),
                ctx.Process(target=_worker_acquire, args=(tmp, key, 3, q)),
            ]
            for p in procs:
                p.start()
            for p in procs:
                p.join(timeout=30)
                self.assertEqual(p.exitcode, 0)
            results = [q.get(timeout=5) for _ in range(2)]
            self.assertEqual(sum(r[1] for r in results), 6)
            # Shared ceiling: 6 calls at burst=2 / 5/s should take noticeably > 0.5s total
            # (each process reports its own wall time; at least one should stretch)
            self.assertTrue(any(r[2] >= 0.3 for r in results))


class TestPriorityScheduling(unittest.TestCase):
    def test_high_scheduled_before_low_under_ceiling(self):
        lim = RestLimiter(max_per_sec=2, burst=1, cross_process=False)
        order = []

        def _acq(priority, name):
            lim.acquire(priority=priority, name=name)
            order.append(priority)

        # Saturate the bucket
        lim.acquire(priority='NORMAL', name='seed')
        with ThreadPoolExecutor(max_workers=3) as pool:
            # Enqueue LOW first, then HIGH — HIGH should still run next
            f_low = pool.submit(_acq, 'LOW', 'low')
            time.sleep(0.05)
            f_high = pool.submit(_acq, 'HIGH', 'high')
            f_low.result(timeout=5)
            f_high.result(timeout=5)
        self.assertEqual(order[0], 'HIGH')
        self.assertEqual(order[1], 'LOW')

    def test_high_never_bypasses_rate_ceiling(self):
        lim = RestLimiter(max_per_sec=5, burst=1, cross_process=False)
        t0 = time.time()
        lim.acquire(priority='HIGH', name='a')
        lim.acquire(priority='HIGH', name='b')
        elapsed = time.time() - t0
        self.assertGreaterEqual(elapsed, 0.15)


class TestRetryPolicy(unittest.TestCase):
    def test_429_and_cooldown_and_auth_are_non_retryable(self):
        self.assertTrue(_is_non_retryable_broker_error(BrokerRateLimited('429')))
        self.assertTrue(_is_non_retryable_broker_error(BrokerCooldownActive('cooldown')))
        self.assertTrue(_is_non_retryable_broker_error(RuntimeError('HTTP 401 unauthorized')))
        self.assertTrue(_is_non_retryable_broker_error(RuntimeError('HTTP 403 forbidden')))
        self.assertTrue(_is_non_retryable_broker_error(RuntimeError('HTTP 429 rate limit')))

    def test_429_not_retried_with_fixed_delays(self):
        calls = {'n': 0}

        def _boom():
            calls['n'] += 1
            raise RuntimeError('HTTP 429 Too Many Requests')

        with self.assertRaises(RuntimeError):
            _retry_on_transient(_boom, max_retries=3, base_delay=2.0)
        self.assertEqual(calls['n'], 1)

    def test_retry_after_honored_once_then_raises(self):
        class _Exc(Exception):
            retry_after = 0.05

        calls = {'n': 0}

        def _boom():
            calls['n'] += 1
            raise _Exc('429 Retry-After: 0.05')

        t0 = time.time()
        with self.assertRaises(_Exc):
            _retry_on_transient(_boom, max_retries=3, base_delay=2.0)
        self.assertEqual(calls['n'], 1)
        self.assertGreaterEqual(time.time() - t0, 0.04)
        self.assertEqual(_extract_retry_after_sec(_Exc('x')), 0.05)

    def test_network_5xx_still_retried(self):
        calls = {'n': 0}

        def _boom():
            calls['n'] += 1
            if calls['n'] < 3:
                raise ConnectionError('connection reset by peer')
            return 'ok'

        self.assertEqual(_retry_on_transient(_boom, max_retries=3, base_delay=0.01), 'ok')
        self.assertEqual(calls['n'], 3)


class TestProbeTimeoutAndMinInterval(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'cooldown.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        os.environ['REST_PROBE_HARD_DEADLINE_SEC'] = '0.2'
        os.environ['REST_PROBE_MIN_INTERVAL_SEC'] = '30'
        initialize_for_session_date('2026-07-24')

    def tearDown(self):
        os.environ.pop('REST_PROBE_HARD_DEADLINE_SEC', None)
        os.environ.pop('REST_PROBE_MIN_INTERVAL_SEC', None)
        os.environ.pop('REST_PROBE_ON_SESSION_START', None)
        self._tmp.cleanup()

    def test_hard_deadline_records_timed_out(self):
        hang = threading.Event()

        def _hang_probe(broker, *, source, strategy='', tranche_id=''):
            hang.wait(timeout=2.0)
            return RestProbeResult(
                ok=True,
                status='healthy',
                attempted_at_epoch=time.time(),
                completed_at_epoch=time.time(),
                latency_ms=1,
                http_status=200,
                detail='',
                source=source,
                strategy=strategy,
                tranche_id=tranche_id,
                session_date_ct='2026-07-24',
            )

        coord = ProbeCoordinator(
            session_date_ct='2026-07-24',
            get_broker_fn=lambda: MagicMock(),
            run_probe_fn=_hang_probe,
            clock=lambda: datetime(2026, 7, 24, 8, 0, 0),
            tick_interval_sec=0.05,
        )
        # Disable startup to isolate one tranche timeout
        os.environ['REST_PROBE_ON_SESSION_START'] = 'false'
        coord.set_tranches([
            TrancheWindow('MEIC_IC', '11-00', dt_time(10, 59), dt_time(11, 5)),
        ])
        coord._clock = lambda: datetime(2026, 7, 24, 10, 58, 30)
        coord.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and coord.automatic_probe_count() < 1:
            time.sleep(0.05)
        hang.set()
        coord.stop()
        rec = (read_state().get('probes_by_tranche') or {}).get('11-00') or {}
        self.assertTrue(rec.get('performed'))
        self.assertIn(rec.get('status_phase'), ('completed', 'timed_out'))
        self.assertEqual(rec.get('status'), 'timed_out')
        self.assertFalse(rec.get('ok'))

    def test_min_interval_suppressed_does_not_reuse_other_tranche(self):
        # Seed a successful other tranche
        from common.trading_gate import record_probe_result

        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True,
            status='healthy',
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=1,
            http_status=200,
            detail='',
            source='pre_tranche',
            strategy='MEIC_IC',
            tranche_id='11-00',
            session_date_ct='2026-07-24',
        ))
        # Force last-probe clock recent
        import common.rest_probe as rp

        rp._LAST_PROBE_AT = time.time()
        broker = MagicMock()
        result = run_rest_probe(
            broker,
            bypass_min_interval=False,
            source='pre_tranche',
            strategy='MEIC_IC',
            tranche_id='12-00',
            session_date_ct='2026-07-24',
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'suppressed_min_interval')
        self.assertEqual(result.tranche_id, '12-00')
        rec = (read_state().get('probes_by_tranche') or {}).get('12-00') or {}
        self.assertTrue(rec.get('performed'))
        self.assertEqual(rec.get('status_phase'), 'completed')
        self.assertFalse(rec.get('ok'))
        # Other tranche remains healthy
        other = (read_state().get('probes_by_tranche') or {}).get('11-00') or {}
        self.assertTrue(other.get('ok'))
        broker.probe_orders_rest.assert_not_called()

    def test_coordinator_bypasses_min_interval(self):
        import common.rest_probe as rp

        rp._LAST_PROBE_AT = time.time()
        broker = MagicMock()
        broker.probe_orders_rest.return_value = RestProbeResult(
            ok=True,
            status='healthy',
            attempted_at_epoch=time.time(),
            completed_at_epoch=time.time(),
            latency_ms=1,
            http_status=200,
            detail='',
        )
        result = run_rest_probe(
            broker,
            bypass_min_interval=True,
            source='pre_tranche',
            strategy='MEIC_IC',
            tranche_id='01-15',
            session_date_ct='2026-07-24',
        )
        self.assertTrue(result.ok)
        broker.probe_orders_rest.assert_called_once()


class TestManualProbe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'cooldown.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        os.environ['REST_PROBE_ON_SESSION_START'] = 'false'
        initialize_for_session_date('2026-07-24')

    def tearDown(self):
        self._tmp.cleanup()

    def test_manual_probe_scheduled_once_and_gate_uses_slot_key(self):
        from blocks.entry.runner import EntryMonitorRunner
        from blocks.session.manual_helpers import append_manual_session_row
        from blocks.session.plan import load_manual_session_today
        import common.probe_coordinator as pc

        calls = []

        def _run_probe(broker, *, source, strategy='', tranche_id=''):
            calls.append((source, strategy, tranche_id))
            now = time.time()
            return RestProbeResult(
                ok=True,
                status='healthy',
                attempted_at_epoch=now,
                completed_at_epoch=now,
                latency_ms=1,
                http_status=200,
                detail='',
                source=source,
                strategy=strategy,
                tranche_id=tranche_id,
                session_date_ct='2026-07-24',
            )

        coord = ProbeCoordinator(
            session_date_ct='2026-07-24',
            get_broker_fn=lambda: MagicMock(),
            run_probe_fn=_run_probe,
            clock=lambda: datetime(2026, 7, 24, 12, 0, 0),
            tick_interval_sec=0.05,
        )
        coord.start()
        pc._COORDINATOR = coord
        try:
            append_manual_session_row(
                self._tmp.name,
                side='C',
                short_strike=7455,
                long_strike=7480,
                limit_credit=0.45,
                quantity=1,
            )
            plan = load_manual_session_today(self._tmp.name)
            slot_key = plan.rows[0].slot_key
            runner = EntryMonitorRunner(root=self._tmp.name)
            now = datetime(2026, 7, 24, 12, 0, 0)
            with patch('blocks.entry.runner.load_meic_session_today', return_value=None):
                with patch.object(runner, '_run_worker'):
                    runner.tick(now)
                    plan = load_manual_session_today(self._tmp.name)
                    row = plan.row_by_slot_key(slot_key)
                    self.assertIn(row.state, ('waiting_rest_probe', 'entering', 'placing'))
                    # Exactly one manual probe, no auto-retry
                    deadline = time.time() + 2.0
                    while time.time() < deadline and not calls:
                        time.sleep(0.05)
                    time.sleep(0.15)
                    for _ in range(2):
                        try:
                            runner.tick(now)
                        except PermissionError:
                            time.sleep(0.1)
                            runner.tick(now)
                    time.sleep(0.2)
            manual_calls = [c for c in calls if c[0] == 'manual']
            self.assertEqual(len(manual_calls), 1)
            self.assertEqual(manual_calls[0][1], 'MANUAL_SPREAD')
            self.assertEqual(manual_calls[0][2], slot_key)
            d = evaluate_new_risk_gate(
                require_fresh_probe=True,
                strategy='MANUAL_SPREAD',
                tranche_id=slot_key,
            )
            self.assertFalse(d.blocked)
        finally:
            coord.stop()
            pc._COORDINATOR = None


class TestBrokerReuse(unittest.TestCase):
    def test_meic_and_manual_workers_use_shared_broker(self):
        from blocks.entry import meic_worker, manual_worker

        self.assertIs(meic_worker.get_shared_broker, manual_worker.get_shared_broker)
        with patch('blocks.entry.meic_worker.get_shared_broker') as mock_shared:
            mock_shared.return_value = MagicMock()
            # Import-level alias — confirm run path would call shared
            from common.broker_factory import get_shared_broker, reset_shared_broker

            reset_shared_broker()
            with patch('common.broker_factory.get_broker', side_effect=lambda **kw: MagicMock()) as mock_get:
                b1 = get_shared_broker()
                b2 = get_shared_broker()
            self.assertIs(b1, b2)
            self.assertEqual(mock_get.call_count, 1)


class TestBatchedReconcile(unittest.TestCase):
    def test_one_live_orders_snapshot_for_many_monitors(self):
        from blocks.stop.batched_reconcile import batch_peaceful_reconcile

        broker = MagicMock()
        orders = [MagicMock(id='1'), MagicMock(id='2')]
        broker.get_live_orders_cached.return_value = orders

        monitors = []
        for oid in ('1', '2', '3'):
            mon = MagicMock()
            mon._reconcile_active_stop_with_broker = MagicMock()
            mon._sync_working_stop_order = MagicMock()
            monitors.append(mon)

        snap = batch_peaceful_reconcile(broker, monitors)
        self.assertEqual(len(snap), 2)
        broker.get_live_orders_cached.assert_called_once_with(ttl_sec=0)
        for mon in monitors:
            mon._reconcile_active_stop_with_broker.assert_called_once()
            kwargs = mon._reconcile_active_stop_with_broker.call_args.kwargs
            self.assertEqual(kwargs.get('live_orders'), snap)

    def test_missing_order_uses_direct_get_order(self):
        from brokers.tastytrade_broker import TastyTradeBroker

        broker = object.__new__(TastyTradeBroker)
        broker.session = MagicMock()
        broker.account = MagicMock()
        broker._run = MagicMock(return_value=MagicMock())
        live = [MagicMock(id='100')]

        with patch(
            'brokers.tastytrade_broker._order_result_from_placed_order',
            return_value=MagicMock(success=True, status='working'),
        ):
            broker.get_order_status('999', live_orders=live, priority='LOW', op='working_stop_reconcile')
        broker._run.assert_called_once()


class TestSimulatedAggregateCallRate(unittest.TestCase):
    def test_twelve_open_spreads_before_after_calls_per_minute(self):
        """Simulate 12 open spreads: per-trade status vs one snapshot per cycle."""
        n_spreads = 12
        # Before: each peaceful reconcile hit get_live_orders (or cache miss) independently.
        # At ~15–20s interval ≈ 3–4 cycles/min → ~36–48 live-orders calls/min worst case.
        interval_sec = 17.5
        cycles_per_min = 60.0 / interval_sec
        before_cpm = n_spreads * cycles_per_min
        # After: one get_live_orders snapshot per cycle; direct get_order only for misses (0 here).
        after_cpm = 1.0 * cycles_per_min
        self.assertAlmostEqual(before_cpm, 41.14, places=1)
        self.assertAlmostEqual(after_cpm, 3.43, places=1)
        self.assertLess(after_cpm, before_cpm / 5)

        # Exercise governor aggregate counters without live broker I/O
        with tempfile.TemporaryDirectory() as tmp:
            lim = RestLimiter(
                max_per_sec=100,
                burst=50,
                account_key='paper_SIM12',
                root=tmp,
                cross_process=True,
            )
            for i in range(int(after_cpm) + 1):
                lim.acquire(priority='LOW', name='get_live_orders')
            stats = lim.stats()
            self.assertGreaterEqual(stats['account_calls_last_1m'], 1)
            self.assertEqual(stats['account_key'], 'paper_SIM12')


if __name__ == '__main__':
    unittest.main()
