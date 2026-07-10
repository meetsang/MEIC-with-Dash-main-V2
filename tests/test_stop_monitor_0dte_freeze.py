"""Stop monitor freezes broker actions for 0DTE after 3 PM CT."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from blocks.stop.monitor import StopMonitor


class TestStopMonitor0DteFreeze(unittest.TestCase):
    def _monitor(self, path: str) -> StopMonitor:
        broker = MagicMock()
        prices = MagicMock()
        prices.get_spx.return_value = 7350.0
        prices.kill_switch = False
        state = {
            'status': 'open',
            'quantity': 1,
            'filled_quantity': 1,
            'stop_quantity': 0,
            'entry': {'side': 'P', 'strategy': 'MEIC_IC'},
            'short_leg': {'symbol': '.SPXW260626P7320', 'strike': 7320, 'fill_price': 2.0},
            'long_leg': {'symbol': '.SPXW260626P7295', 'strike': 7295, 'fill_price': 0.5},
            'phases': {},
            'recovery': {
                'module_start_count': 0,
                'last_heartbeat': '2026-06-26T10:00:00-05:00',
                'state_loaded_from_disk': False,
            },
        }
        with patch('blocks.stop.monitor.state_mod.load_state', return_value=state):
            return StopMonitor(path, broker, prices, phases=[])

    def test_place_short_stop_blocked_after_close(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        monitor = self._monitor(path)
        with patch('blocks.stop.monitor.try_settle_or_freeze_trade', return_value=('ok', monitor.state)), \
             patch('blocks.stop.monitor.broker_actions_allowed_for_trade', return_value=(False, 'expired_option')), \
             patch('blocks.stop.monitor.state_mod.save_state'):
            ok = monitor._place_short_stop(4.0, 4.1, phase=1, reason='test')
        self.assertFalse(ok)
        monitor.broker.place_stop_order.assert_not_called()

    def test_poll_once_skips_ensure_stop_after_close(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        monitor = self._monitor(path)
        with patch.object(monitor, '_0dte_past_market_close', return_value=True):
            with patch.object(monitor, '_ensure_stop_for_filled_qty') as ensure:
                with patch('blocks.stop.monitor.state_mod.save_state'):
                    monitor._poll_once()
        ensure.assert_not_called()


if __name__ == '__main__':
    unittest.main()
