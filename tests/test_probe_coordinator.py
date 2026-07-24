"""Probe coordinator budget and non-blocking gate tests."""
from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock

from common.probe_coordinator import ProbeCoordinator, TrancheWindow, meic_tranches_from_slots
from common.rest_probe import RestProbeResult
from common.trading_gate import (
    GateDecision,
    evaluate_new_risk_gate,
    initialize_for_session_date,
    mark_probe_scheduled,
    read_state,
    record_probe_result,
)
from orchestrator.scheduler import TrancheSlot


class TestProbeCoordinator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'cooldown.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        os.environ['REST_PROBE_COORDINATOR_ENABLED'] = 'true'
        os.environ['REST_PROBE_ON_SESSION_START'] = 'true'
        os.environ['PRE_TRANCHE_PROBE_LEAD_SEC'] = '30'
        os.environ['REST_PROBE_MIN_INTERVAL_SEC'] = '0'
        os.environ['REST_PROBE_HARD_DEADLINE_SEC'] = '30'
        initialize_for_session_date('2026-07-13')
        self._calls = []
        self._clock = {'now': datetime(2026, 7, 13, 8, 0, 0)}

    def tearDown(self):
        self._tmp.cleanup()

    def _clock_fn(self):
        return self._clock['now']

    def _make_result(self, *, source, strategy='', tranche_id='', ok=True):
        now = time.time()
        return RestProbeResult(
            ok=ok,
            status='healthy' if ok else 'unavailable',
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=10,
            http_status=200 if ok else None,
            detail='' if ok else 'fail',
            source=source,
            strategy=strategy,
            tranche_id=tranche_id,
            session_date_ct='2026-07-13',
            performed=True,
        )

    def _run_probe(self, broker, *, source, strategy='', tranche_id=''):
        self._calls.append((source, strategy, tranche_id))
        return self._make_result(source=source, strategy=strategy, tranche_id=tranche_id)

    def test_one_startup_and_one_per_tranche_budget(self):
        slots = [
            TrancheSlot('11-00', dt_time(10, 59), dt_time(11, 5), 'MEIC_IC'),
            TrancheSlot('12-00', dt_time(11, 59), dt_time(12, 5), 'MEIC_IC'),
            TrancheSlot('12-30', dt_time(12, 29), dt_time(12, 35), 'MEIC_IC'),
            TrancheSlot('01-15', dt_time(13, 14), dt_time(13, 20), 'MEIC_IC'),
            TrancheSlot('01-45', dt_time(13, 44), dt_time(13, 50), 'MEIC_IC'),
            TrancheSlot('02-00', dt_time(13, 59), dt_time(14, 5), 'MEIC_IC'),
        ]
        coord = ProbeCoordinator(
            session_date_ct='2026-07-13',
            get_broker_fn=lambda: MagicMock(),
            run_probe_fn=self._run_probe,
            clock=self._clock_fn,
            tick_interval_sec=0.05,
        )
        coord.set_tranches(meic_tranches_from_slots(slots))
        coord.start()
        time.sleep(0.3)
        self.assertEqual(sum(1 for c in self._calls if c[0] == 'startup'), 1)

        # Advance through each lead time
        for h, m, s in (
            (10, 58, 30),
            (11, 58, 30),
            (12, 28, 30),
            (13, 13, 30),
            (13, 43, 30),
            (13, 58, 30),
        ):
            self._clock['now'] = datetime(2026, 7, 13, h, m, s)
            time.sleep(0.15)
        deadline = time.time() + 3.0
        while time.time() < deadline and coord.automatic_probe_count() < 7:
            time.sleep(0.05)
        coord.stop()
        self.assertEqual(coord.automatic_probe_count(), 7)
        pre = [c for c in self._calls if c[0] == 'pre_tranche']
        self.assertEqual(len(pre), 6)
        # Dedup: one call per tranche_id
        ids = [c[2] for c in pre]
        self.assertEqual(len(ids), len(set(ids)))

    def test_p_and_c_share_one_probe_record(self):
        record_probe_result(self._make_result(source='pre_tranche', strategy='MEIC_IC', tranche_id='11-00'))
        d_p = evaluate_new_risk_gate(require_fresh_probe=True, strategy='MEIC_IC', tranche_id='11-00')
        d_c = evaluate_new_risk_gate(require_fresh_probe=True, strategy='MEIC_IC', tranche_id='11-00')
        self.assertFalse(d_p.blocked)
        self.assertFalse(d_c.blocked)
        self.assertEqual(len((read_state().get('probes_by_tranche') or {})), 1)

    def test_failed_probe_blocks_both_sides_no_retry_on_ticks(self):
        record_probe_result(self._make_result(
            source='pre_tranche', strategy='MEIC_IC', tranche_id='11-00', ok=False,
        ))
        for _ in range(5):
            d = evaluate_new_risk_gate(require_fresh_probe=True, strategy='MEIC_IC', tranche_id='11-00')
            self.assertTrue(d.blocked)
        # Still only one tranche record
        self.assertEqual(list((read_state().get('probes_by_tranche') or {}).keys()), ['11-00'])

    def test_pending_probe_blocks_with_rest_probe_pending(self):
        mark_probe_scheduled(
            source='pre_tranche',
            session_date_ct='2026-07-13',
            strategy='MEIC_IC',
            tranche_id='11-00',
        )
        d = evaluate_new_risk_gate(require_fresh_probe=True, strategy='MEIC_IC', tranche_id='11-00')
        self.assertTrue(d.blocked)
        self.assertEqual(d.reason, 'rest_probe_pending')

    def test_evaluate_never_calls_broker(self):
        mark_probe_scheduled(
            source='pre_tranche',
            session_date_ct='2026-07-13',
            strategy='MEIC_IC',
            tranche_id='11-00',
        )
        with unittest.mock.patch('common.broker_factory.get_broker') as mock_get, \
             unittest.mock.patch('common.broker_factory.get_shared_broker') as mock_shared, \
             unittest.mock.patch('common.rest_probe.run_rest_probe') as mock_probe:
            evaluate_new_risk_gate(require_fresh_probe=True, strategy='MEIC_IC', tranche_id='11-00')
            mock_get.assert_not_called()
            mock_shared.assert_not_called()
            mock_probe.assert_not_called()

    def test_hanging_probe_does_not_block_coordinator_loop(self):
        hang = threading.Event()

        def _hang_probe(broker, *, source, strategy='', tranche_id=''):
            hang.wait(timeout=2.0)
            return self._make_result(source=source, strategy=strategy, tranche_id=tranche_id)

        coord = ProbeCoordinator(
            session_date_ct='2026-07-13',
            get_broker_fn=lambda: MagicMock(),
            run_probe_fn=_hang_probe,
            clock=self._clock_fn,
            tick_interval_sec=0.05,
        )
        coord.set_tranches([
            TrancheWindow('MEIC_IC', '11-00', dt_time(10, 59), dt_time(11, 5)),
        ])
        # Startup will hang — coordinator loop must keep ticking
        t0 = time.time()
        coord.start()
        self._clock['now'] = datetime(2026, 7, 13, 10, 58, 30)
        time.sleep(0.4)
        # Loop still alive (thread running) despite hanging startup
        self.assertTrue(coord._thread is not None and coord._thread.is_alive())
        hang.set()
        deadline = time.time() + 2.0
        while time.time() < deadline and coord.automatic_probe_count() < 2:
            time.sleep(0.05)
        coord.stop()
        self.assertLess(time.time() - t0, 5.0)
        self.assertGreaterEqual(coord.automatic_probe_count(), 1)

    def test_repeated_ticks_do_not_create_extra_probes(self):
        coord = ProbeCoordinator(
            session_date_ct='2026-07-13',
            get_broker_fn=lambda: MagicMock(),
            run_probe_fn=self._run_probe,
            clock=self._clock_fn,
            tick_interval_sec=0.05,
        )
        coord.set_tranches([
            TrancheWindow('MEIC_IC', '11-00', dt_time(10, 59), dt_time(11, 5)),
        ])
        coord.start()
        self._clock['now'] = datetime(2026, 7, 13, 10, 58, 30)
        time.sleep(0.5)
        # Sit in window with many coordinator ticks
        self._clock['now'] = datetime(2026, 7, 13, 11, 0, 0)
        time.sleep(0.5)
        coord.stop()
        pre = [c for c in self._calls if c[0] == 'pre_tranche' and c[2] == '11-00']
        self.assertEqual(len(pre), 1)
        self.assertEqual(sum(1 for c in self._calls if c[0] == 'startup'), 1)


# import mock at module for test_evaluate
import unittest.mock  # noqa: E402


class TestTrancheMissed(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        initialize_for_session_date('2026-07-13')

    def tearDown(self):
        self._tmp.cleanup()

    def test_missed_exactly_once(self):
        from blocks.entry.runner import EntryMonitorRunner
        from blocks.session.bootstrap import bootstrap_meic_session_if_missing
        from blocks.session.plan import load_meic_session_today

        bootstrap_meic_session_if_missing(
            self._tmp.name,
            slots=[TrancheSlot('11-00', dt_time(10, 59), dt_time(11, 5))],
        )
        runner = EntryMonitorRunner(root=self._tmp.name, logger=logging.getLogger('t'))
        now = datetime(2026, 7, 13, 11, 6, 0)
        with self.assertLogs('t', level='CRITICAL') as cm:
            runner.tick(now)
            runner.tick(now)
        missed = [line for line in cm.output if 'TRANCHE_MISSED' in line]
        # two sides → two slot_keys, each once across two ticks
        self.assertEqual(len(missed), 2)


if __name__ == '__main__':
    unittest.main()
