"""Stop runner applies expiry gate before starting monitors."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.runner import MonitorRunner


def _expired_open_trade() -> dict:
    st = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot='11-00',
        side='P',
        short_symbol='.SPXW260708P7320',
        long_symbol='.SPXW260708P7295',
        short_strike=7320,
        long_strike=7295,
        short_fill=2.0,
        long_fill=0.5,
        net_credit=1.4,
        quantity=1,
        open_order_id='481500001',
    )
    return st


class TestStopRunnerExpiryGate(unittest.TestCase):
    def test_add_skips_monitor_for_settled_expired_trade(self):
        broker = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, 'expired.json')
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(_expired_open_trade(), f)
            runner = MonitorRunner(broker, watch_dir=tmp)
            with patch(
                'blocks.stop.expiry_gate.get_spx_settlement_close',
                return_value=7471.32,
            ):
                with patch('blocks.stop.runner.StopMonitor') as mock_mon:
                    runner.add(fpath)
                    mock_mon.assert_not_called()
            with open(fpath, encoding='utf-8') as f:
                saved = json.load(f)
            self.assertEqual(saved['status'], 'closed')
            self.assertEqual(saved['close_mechanism'], 'expiry_settlement')

    def test_add_skips_monitor_for_frozen_expired_trade(self):
        broker = MagicMock()
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, 'expired.json')
            with open(fpath, 'w', encoding='utf-8') as f:
                json.dump(_expired_open_trade(), f)
            runner = MonitorRunner(broker, watch_dir=tmp)
            with patch('blocks.stop.expiry_gate.ensure_spx_settlement_close', return_value=None):
                with patch('blocks.stop.expiry_gate.get_spx_settlement_close', return_value=None):
                    with patch('blocks.stop.runner.StopMonitor') as mock_mon:
                        runner.add(fpath)
                        mock_mon.assert_not_called()
            with open(fpath, encoding='utf-8') as f:
                saved = json.load(f)
            self.assertEqual(saved['status'], 'open')
            self.assertTrue(saved['expiry_settlement_pending'])
            self.assertTrue(saved['broker_actions_frozen'])


if __name__ == '__main__':
    unittest.main()
