"""run.py --one-day launcher lifecycle tests."""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

import run as launcher


class TestRunOneDay(unittest.TestCase):
    def test_default_main_module_has_persistent_loop(self):
        import inspect
        src = inspect.getsource(launcher)
        self.assertIn('while True:', src)
        self.assertIn('one_day', src)

    def test_one_day_branch_skips_weekly_sleep(self):
        import inspect
        src = inspect.getsource(launcher)
        self.assertIn('elif one_day:', src)
        self.assertNotIn('elif one_day:\n            try:\n                run_session_cleanup', src.replace(' ', ''))

    def test_argparse_one_day_flag(self):
        with patch.object(sys, 'argv', ['run.py', '--one-day']):
            parser = __import__('argparse').ArgumentParser()
            parser.add_argument('--one-day', action='store_true')
            args = parser.parse_args(['--one-day'])
            self.assertTrue(args.one_day)

    def test_startup_probe_failure_still_starts_services(self):
        now = datetime(2026, 7, 10, 8, 35, 0)
        streamer = MagicMock(poll=MagicMock(return_value=None), terminate=MagicMock(), wait=MagicMock())
        market_data = MagicMock(poll=MagicMock(return_value=None), terminate=MagicMock(), wait=MagicMock())
        stop_flags = {'stop': False}

        def _should_stop(session_started, now, profile=None):
            return stop_flags['stop']

        with patch.object(launcher, '_central_now', return_value=now), \
             patch.object(launcher, 'validate_startup_config'), \
             patch.object(launcher, 'check_trading_day', return_value=(True, 'ok')), \
             patch('common.trading_gate.initialize_for_session_date'), \
             patch('common.probe_coordinator.start_coordinator'), \
             patch('common.probe_coordinator.stop_coordinator'), \
             patch('common.entry_coordinator.start_entry_coordinator'), \
             patch('common.entry_coordinator.stop_entry_coordinator'), \
             patch.object(launcher, 'wait_until'), \
             patch.object(launcher, 'runtime_should_stop_for_session', side_effect=_should_stop), \
             patch.object(launcher, '_run_eod_cleanup_if_due'), \
             patch.object(launcher, '_write_status'), \
             patch.object(launcher, 'start_streamer', return_value=streamer) as mock_stream, \
             patch.object(launcher, 'start_market_data_recorder', return_value=market_data), \
             patch.object(launcher, 'start_stop_monitor', return_value=None), \
             patch.object(launcher, 'load_enabled_strategies', return_value=[]), \
             patch.object(launcher, 'bootstrap_meic_session_if_missing'), \
             patch.object(launcher, 'EntryMonitorRunner'), \
             patch.object(launcher, 'time') as mock_time:
            mock_time.sleep = MagicMock(side_effect=lambda s: stop_flags.__setitem__('stop', True))
            mock_time.monotonic = MagicMock(return_value=100.0)
            launcher.main(force=True, no_stop_monitor=True)
        mock_stream.assert_called_once()

    def test_holiday_skips_trading(self):
        now = datetime(2026, 7, 10, 8, 0, 0)
        with patch.object(launcher, '_central_now', return_value=now), \
             patch.object(launcher, 'validate_startup_config'), \
             patch.object(launcher, 'check_trading_day', return_value=(False, 'FOMC')), \
             patch.object(launcher, '_write_status') as mock_status, \
             patch.object(launcher, 'start_streamer') as mock_stream:
            launcher.main()
        mock_stream.assert_not_called()
        mock_status.assert_called()

    def test_session_closed_before_start_skips_entries(self):
        now = datetime(2026, 7, 10, 16, 0, 0)
        with patch.object(launcher, '_central_now', return_value=now), \
             patch.object(launcher, 'validate_startup_config'), \
             patch.object(launcher, 'check_trading_day', return_value=(True, 'ok')), \
             patch('common.trading_gate.initialize_for_session_date'), \
             patch('common.probe_coordinator.start_coordinator'), \
             patch('common.probe_coordinator.stop_coordinator'), \
             patch.object(launcher, 'wait_until'), \
             patch.object(launcher, 'runtime_should_stop_for_session', return_value=True), \
             patch.object(launcher, '_run_eod_cleanup_if_due'), \
             patch.object(launcher, '_write_status'), \
             patch.object(launcher, 'start_streamer') as mock_stream, \
             patch.object(launcher, 'EntryMonitorRunner') as mock_runner:
            launcher.main(force=False, tranche_now=False)
        mock_stream.assert_not_called()
        mock_runner.assert_not_called()

    def test_launcher_lock_prevents_duplicate(self):
        from common.process_lock import acquire_lock, release_lock
        name = f'launcher-test-dup-{os.getpid()}'
        release_lock(name)
        self.assertTrue(acquire_lock(name, command='test'))
        with patch('common.process_lock._pid_alive', return_value=True), \
             patch('common.process_lock.read_lock', return_value={'pid': 99999}):
            self.assertFalse(acquire_lock(name, command='test2'))
        release_lock(name)

    def test_wait_until_central_blocks_until_target(self):
        calls = {'n': 0}
        targets = [
            datetime(2026, 7, 10, 8, 0, 0),
            datetime(2026, 7, 10, 8, 0, 1),
        ]

        def _now():
            calls['n'] += 1
            return targets[min(calls['n'] - 1, 1)]

        with patch.object(launcher, '_central_now', side_effect=_now), \
             patch.object(launcher, 'time') as mock_time:
            mock_time.sleep = MagicMock()
            launcher.wait_until_central(datetime(2026, 7, 10, 8, 0, 1))
        self.assertGreaterEqual(calls['n'], 2)


if __name__ == '__main__':
    unittest.main()
