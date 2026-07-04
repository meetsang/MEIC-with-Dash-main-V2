"""Stop runner ignores pending_fill and partial fills."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.runner import MonitorRunner


class TestStopRunnerGate(unittest.TestCase):
    def test_scan_syncs_pending_fills_before_add(self):
        broker = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            runner = MonitorRunner(broker, watch_dir=tmp)
            with patch('blocks.stop.runner.sync_pending_fills') as mock_sync:
                runner._scan_for_new()
            mock_sync.assert_called_once_with(broker)

    def test_skips_pending_fill(self):
        broker = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            runner = MonitorRunner(broker, watch_dir=tmp)
            state = state_mod.create_pending_state(
                strategy='MEIC_IC',
                lot='11-00',
                side='P',
                short_symbol='.SPXW260625P7000',
                long_symbol='.SPXW260625P6975',
                short_strike=7000,
                long_strike=6975,
                target_quantity=1,
                open_order_id='123',
                limit_credit=1.0,
            )
            fpath = os.path.join(tmp, 'test.json')
            state_mod.save_state(fpath, state)
            with patch('blocks.stop.runner.StopMonitor') as mock_mon:
                runner.add(fpath)
                mock_mon.assert_not_called()


if __name__ == '__main__':
    unittest.main()
