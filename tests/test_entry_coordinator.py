"""Entry monitor background coordinator tests."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, time as dt_time
from unittest.mock import patch

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from common.entry_coordinator import (
    EntryMonitorCoordinator,
    coordinator_enabled,
    start_entry_coordinator,
    stop_entry_coordinator,
    write_entry_monitor_health,
)
from common.trading_gate import initialize_for_session_date
from orchestrator.scheduler import TrancheSlot


class TestEntryCoordinator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'broker_cooldown.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        os.environ['ENTRY_MONITOR_COORDINATOR_ENABLED'] = 'true'
        os.environ['ENTRY_MONITOR_TICK_INTERVAL_SEC'] = '0.05'
        os.environ['ENTRY_MONITOR_HEARTBEAT_LOG_SEC'] = '9999'
        initialize_for_session_date('2026-07-21')
        bootstrap_meic_session_if_missing(
            self._tmp.name,
            slots=[TrancheSlot('11-00', dt_time(10, 59), dt_time(11, 5))],
        )

    def tearDown(self):
        stop_entry_coordinator()
        self._tmp.cleanup()

    def test_background_tick_spawns_in_window(self):
        runner = EntryMonitorRunner(root=self._tmp.name)
        clock = {'now': datetime(2026, 7, 21, 11, 0, 0)}

        with patch.object(runner, '_run_worker'):
            with patch('blocks.entry.runner.evaluate_new_risk_gate') as mock_gate:
                from common.trading_gate import GateDecision

                mock_gate.return_value = GateDecision(blocked=False)
                coord = EntryMonitorCoordinator(
                    runner,
                    root=self._tmp.name,
                    clock=lambda: clock['now'],
                    interval_sec=0.05,
                )
                coord.start()
                time.sleep(0.25)
                coord.stop()
        self.assertIn('11-00_P', runner._fired)

    def test_stall_check_logs_when_tick_hangs(self):
        runner = EntryMonitorRunner(root=self._tmp.name)
        coord = EntryMonitorCoordinator(runner, root=self._tmp.name, interval_sec=0.05)
        with coord._stats_lock:
            coord._last_tick_completed_mono = time.monotonic() - 60.0
            coord._last_tick_duration_sec = 0.01
        with self.assertLogs('common.entry_coordinator', level='CRITICAL') as cm:
            coord.check_stall()
        self.assertTrue(any('ENTRY_MONITOR_STALL' in line for line in cm.output))

    def test_start_stop_singleton(self):
        runner = EntryMonitorRunner(root=self._tmp.name)
        c1 = start_entry_coordinator(runner, root=self._tmp.name)
        c2 = start_entry_coordinator(runner, root=self._tmp.name)
        self.assertIsNotNone(c1)
        self.assertIsNotNone(c2)
        stop_entry_coordinator()

    def test_write_health_file(self):
        write_entry_monitor_health(
            root=self._tmp.name,
            tick_count=3,
            last_tick_duration_sec=0.012,
            pending_meic=2,
            active_workers=0,
        )
        path = os.path.join(self._tmp.name, 'trades', 'entry_monitor_health.json')
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as f:
            text = f.read()
        self.assertIn('"tick_count": 3', text)

    def test_coordinator_enabled_default(self):
        os.environ.pop('ENTRY_MONITOR_COORDINATOR_ENABLED', None)
        self.assertTrue(coordinator_enabled())


if __name__ == '__main__':
    unittest.main()
