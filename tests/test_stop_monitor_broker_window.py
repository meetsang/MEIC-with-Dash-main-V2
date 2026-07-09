"""Stop monitor respects MEIC/SPX RTH broker-action window."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from blocks.stop.monitor import StopMonitor
from brokers.base import OrderResult


class TestStopMonitorBrokerWindow(unittest.TestCase):
    def _state(self, expiry: str = '260710') -> dict:
        return {
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'stop_quantity': 0,
            'entry': {'side': 'P', 'strategy': 'MEIC_IC'},
            'short_leg': {
                'symbol': f'.SPXW{expiry}P7435',
                'strike': 7435,
                'fill_price': 2.0,
            },
            'long_leg': {
                'symbol': f'.SPXW{expiry}P7410',
                'strike': 7410,
                'fill_price': 0.8,
            },
            'phases': {},
        }

    def _monitor(self, path: str, state: dict) -> StopMonitor:
        broker = MagicMock()
        prices = MagicMock()
        prices.get_spx.return_value = 7350.0
        prices.kill_switch = False
        with patch('blocks.stop.monitor.state_mod.load_state', return_value=state):
            return StopMonitor(path, broker, prices, phases=[])

    def test_1dte_overnight_pauses_without_broker_call(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        state = self._state('260710')
        monitor = self._monitor(path, state)
        with patch('blocks.stop.monitor.broker_actions_allowed_for_trade', return_value=(False, 'outside_meic_spx_broker_action_window')):
            with patch('blocks.stop.monitor.state_mod.save_state'):
                monitor._poll_once()
        monitor.broker.place_stop_order.assert_not_called()
        self.assertTrue(monitor.state.get('broker_actions_paused'))
        self.assertEqual(
            monitor.state.get('broker_actions_pause_reason'),
            'outside_meic_spx_broker_action_window',
        )
        self.assertTrue(monitor.state.get('stop_rearm_pending'))

    def test_1dte_window_open_rearms_stop(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        state = self._state('260710')
        state['stop_rearm_pending'] = True
        monitor = self._monitor(path, state)
        monitor.broker.place_stop_order.return_value = OrderResult(
            True, '999', 'working',
        )
        with patch('blocks.stop.monitor.broker_actions_allowed_for_trade', return_value=(True, 'allowed')):
            with patch.object(monitor, '_ensure_stop_for_filled_qty_unblocked') as ensure:
                monitor._maybe_rearm_after_window_open()
                ensure.assert_called_once()

    def test_expired_trade_uses_expired_option_not_window_pause(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        state = self._state('260708')
        monitor = self._monitor(path, state)
        with patch('blocks.stop.monitor.broker_actions_allowed_for_trade', return_value=(False, 'expired_option')):
            with patch('blocks.stop.monitor.state_mod.save_state'):
                blocked = monitor._broker_actions_blocked()
        self.assertTrue(blocked)
        self.assertNotIn('broker_actions_paused', monitor.state)
        monitor.broker.place_stop_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
