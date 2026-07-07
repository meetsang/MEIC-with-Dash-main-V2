"""Broker traffic hardening — dashboard, shared broker, locks, limiter, cooldown."""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common import broker_cooldown
from common.broker_factory import get_shared_broker, reset_shared_broker, shared_broker_stats
from common.process_lock import acquire_lock, list_locks, read_lock, release_lock
from common.rest_limiter import RestLimiter, reset_rest_limiter
from dashboard.broker_fill_sync import maybe_sync_active_trades, reset_fill_sync_state, fill_sync_stats
from tests.mock_broker import MockBroker


class TestSharedBroker(unittest.TestCase):
    def setUp(self):
        reset_shared_broker()

    def tearDown(self):
        reset_shared_broker()

    def test_get_shared_broker_reuses_session_in_same_process(self):
        created = []

        def _fake_broker(**_kwargs):
            b = MockBroker()
            created.append(b)
            return b

        with patch('common.broker_factory.get_broker', side_effect=_fake_broker):
            b1 = get_shared_broker()
            b2 = get_shared_broker()
        self.assertIs(b1, b2)
        self.assertEqual(len(created), 1)
        self.assertGreaterEqual(shared_broker_stats()['reuse_count'], 2)

    def test_get_shared_broker_recreates_after_reset(self):
        with patch('common.broker_factory.get_broker', side_effect=lambda **_kw: MockBroker()):
            b1 = get_shared_broker()
            reset_shared_broker()
            b2 = get_shared_broker()
        self.assertIsNot(b1, b2)


class TestDashboardFillSync(unittest.TestCase):
    def setUp(self):
        reset_fill_sync_state()
        broker_cooldown.clear_cooldown()

    def tearDown(self):
        reset_fill_sync_state()
        broker_cooldown.clear_cooldown()

    def _pending_state(self):
        return {
            'status': 'open',
            'filled_quantity': 0,
            'quantity': 3,
            'open_order_id': 'oid-1',
        }

    def test_dashboard_summary_no_broker_when_no_pending_fills(self):
        broker = MockBroker()
        with patch('common.broker_factory.get_broker', return_value=broker):
            maybe_sync_active_trades(
                read_json=lambda p: {'status': 'open', 'filled_quantity': 3, 'quantity': 3},
                iter_paths=lambda: ['t.json'],
                get_broker_fn=lambda: broker,
                sync_fn=MagicMock(),
            )
        self.assertEqual(broker.placed, [])

    def test_dashboard_sync_uses_cached_broker_once(self):
        calls = []

        def _get_broker():
            calls.append(1)
            return MockBroker()

        sync = MagicMock()
        paths = ['a.json', 'b.json']
        maybe_sync_active_trades(
            read_json=lambda p: self._pending_state(),
            iter_paths=lambda: iter(paths),
            get_broker_fn=_get_broker,
            sync_fn=sync,
        )
        maybe_sync_active_trades(
            read_json=lambda p: self._pending_state(),
            iter_paths=lambda: iter(paths),
            get_broker_fn=_get_broker,
            sync_fn=sync,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(sync.call_count, 2)

    def test_dashboard_broker_error_sets_cooldown(self):
        broker = MockBroker()

        def _boom(_b):
            raise RuntimeError('429 rate limit exceeded')

        maybe_sync_active_trades(
            read_json=lambda p: self._pending_state(),
            iter_paths=lambda: ['t.json'],
            get_broker_fn=lambda: broker,
            sync_fn=_boom,
        )
        stats = fill_sync_stats()
        self.assertGreater(stats['cooldown_until'], time.time())
        self.assertIn('429', stats['last_broker_error'])

    def test_dashboard_skips_broker_during_cooldown(self):
        broker_cooldown.set_cooldown('test', source='unit', duration_sec=60)
        sync = MagicMock()
        maybe_sync_active_trades(
            read_json=lambda p: self._pending_state(),
            iter_paths=lambda: ['t.json'],
            get_broker_fn=lambda: MockBroker(),
            sync_fn=sync,
        )
        sync.assert_not_called()


class TestProcessLocks(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._locks = os.path.join(self._tmpdir, 'locks')
        os.makedirs(self._locks, exist_ok=True)
        self._patch = patch('common.process_lock.LOCKS_DIR', self._locks)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_launcher_singleton_lock(self):
        self.assertTrue(acquire_lock('launcher', command='test'))
        self.assertFalse(acquire_lock('launcher', command='test2'))
        release_lock('launcher')
        self.assertTrue(acquire_lock('launcher', command='test3'))

    def test_stale_lock_is_replaced(self):
        path = os.path.join(self._locks, 'launcher.lock')
        with open(path, 'w', encoding='utf-8') as f:
            import json
            json.dump({'pid': 999999, 'name': 'launcher'}, f)
        self.assertTrue(acquire_lock('launcher', command='fresh'))
        meta = read_lock('launcher')
        self.assertEqual(meta['pid'], os.getpid())

    def test_lock_removed_on_release(self):
        acquire_lock('stop_monitor', command='x')
        release_lock('stop_monitor')
        locks = list_locks()
        self.assertEqual(len(locks), 0)


class TestRestLimiter(unittest.TestCase):
    def test_rest_limiter_serializes_burst(self):
        lim = RestLimiter(max_per_sec=10, burst=1)
        t0 = time.monotonic()
        lim.acquire(priority='LOW', name='a')
        lim.acquire(priority='LOW', name='b')
        self.assertGreaterEqual(time.monotonic() - t0, 0.05)


class TestLiveBrokerGate(unittest.TestCase):
    def test_unit_tests_cannot_create_live_broker_without_opt_in(self):
        from common.broker_factory import get_broker
        with self.assertRaises(RuntimeError):
            get_broker(_test_override=False)

    def test_opt_in_allows_get_broker_in_tests(self):
        from common.broker_factory import get_broker
        os.environ['MEIC_ALLOW_LIVE_BROKER_TESTS'] = '1'
        try:
            with patch('common.broker_factory.tt_config.BROKER', 'schwab'):
                with self.assertRaises(NotImplementedError):
                    get_broker(_test_override=False)
        finally:
            os.environ.pop('MEIC_ALLOW_LIVE_BROKER_TESTS', None)


class TestBrokerCooldown(unittest.TestCase):
    def setUp(self):
        broker_cooldown.clear_cooldown()
        self._patch = patch(
            'common.broker_cooldown.DEFAULT_COOLDOWN_PATH',
            os.path.join(tempfile.mkdtemp(), 'broker_cooldown.json'),
        )
        self._path = self._patch.start()

    def tearDown(self):
        self._patch.stop()
        broker_cooldown.clear_cooldown()

    def test_broker_cooldown_skips_low_priority(self):
        broker_cooldown.set_cooldown('429', source='test', duration_sec=30)
        self.assertTrue(broker_cooldown.should_skip_priority('LOW'))
        self.assertFalse(broker_cooldown.should_skip_priority('HIGH'))


class TestTastyLiveOrdersCache(unittest.TestCase):
    def test_get_order_status_uses_cached_live_orders_snapshot(self):
        from brokers.tastytrade_broker import TastyTradeBroker

        broker = object.__new__(TastyTradeBroker)
        broker._live_orders_cache = None
        broker._live_orders_ts = 0.0
        broker._live_orders_ttl = 2.0
        broker._loop = None

        order = MagicMock()
        order.id = '123'
        calls = {'n': 0}

        def _cached(ttl_sec=None):
            calls['n'] += 1
            return [order]

        broker.get_live_orders_cached = _cached
        broker._run = MagicMock()

        from brokers.tastytrade_broker import _order_result_from_placed_order
        with patch('brokers.tastytrade_broker._order_result_from_placed_order', return_value='ok'):
            broker.get_order_status('123')
            broker.get_order_status('123')
        self.assertEqual(calls['n'], 2)


if __name__ == '__main__':
    unittest.main()
