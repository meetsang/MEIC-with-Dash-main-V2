"""Terminal broker stopped-trading classifier."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop.monitor import StopMonitor
from blocks.stop import state as state_mod
from common.broker_stopped_trading import is_instruments_stopped_trading_error
from tests.fill_sync_fixtures import spxw_option_symbol, today_expiry_yymmdd


def _intraday_open_trade() -> dict:
    expiry = today_expiry_yymmdd()
    short_sym = spxw_option_symbol(7320, 'P', expiry_yymmdd=expiry)
    long_sym = spxw_option_symbol(7295, 'P', expiry_yymmdd=expiry)
    return {
        'status': 'open',
        'quantity': 1,
        'filled_quantity': 1,
        'stop_quantity': 0,
        'spread_type': 'credit',
        'entry': {'side': 'P', 'net_credit': 1.4, 'strategy': 'MEIC_IC'},
        'short_leg': {
            'symbol': short_sym,
            'strike': 7320,
            'fill_price': 2.0,
            'two_x_short': 4.0,
        },
        'long_leg': {
            'symbol': long_sym,
            'strike': 7295,
            'fill_price': 0.5,
        },
        'phases': {},
        'recovery': {
            'module_start_count': 0,
            'last_heartbeat': state_mod.now_iso(),
            'state_loaded_from_disk': False,
        },
    }


class TestBrokerStoppedTradingClassifier(unittest.TestCase):
    def test_detects_markers(self):
        self.assertTrue(
            is_instruments_stopped_trading_error(
                'instruments_stopped_trading: symbol halted'
            )
        )
        self.assertTrue(is_instruments_stopped_trading_error('Stopped symbols: .SPXW'))
        self.assertTrue(is_instruments_stopped_trading_error('broker stopped trading now'))
        self.assertFalse(is_instruments_stopped_trading_error('insufficient buying power'))


class TestBrokerStoppedTradingMonitor(unittest.TestCase):
    def _monitor(self, path: str, broker: MagicMock) -> StopMonitor:
        prices = MagicMock()
        prices.get_spx.return_value = 7350.0
        prices.kill_switch = False
        with patch('blocks.stop.monitor.state_mod.load_state', return_value=_intraday_open_trade()):
            return StopMonitor(path, broker, prices, phases=[])

    def test_terminal_error_sets_persistent_flags(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        broker = MagicMock()
        broker.place_stop_order.return_value = OrderResult(
            success=False,
            order_id=None,
            status='rejected',
            message='instruments_stopped_trading: .SPXW260708P7320',
        )
        monitor = self._monitor(path, broker)
        with patch.object(monitor, '_broker_actions_blocked', return_value=False):
            with patch('blocks.stop.monitor.state_mod.save_state') as save:
                monitor._place_short_stop(4.0, 4.1, phase=1, reason='test')
                save.assert_called()
        self.assertTrue(monitor.state['broker_actions_disabled'])
        self.assertEqual(
            monitor.state['broker_actions_disabled_reason'],
            'instruments_stopped_trading',
        )
        self.assertTrue(monitor.state['expiry_settlement_pending'])
        self.assertEqual(monitor._stop_place_backoff_until, 0.0)

    def test_second_poll_does_not_retry_broker(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        broker = MagicMock()
        broker.place_stop_order.return_value = OrderResult(
            success=False,
            order_id=None,
            status='rejected',
            message='instruments_stopped_trading: halted',
        )
        monitor = self._monitor(path, broker)
        with patch.object(monitor, '_broker_actions_blocked', return_value=False):
            with patch('blocks.stop.monitor.state_mod.save_state'):
                monitor._place_short_stop(4.0, 4.1, phase=1, reason='test')
        broker.place_stop_order.reset_mock()
        with patch('blocks.stop.monitor.state_mod.save_state'):
            monitor._ensure_stop_for_filled_qty()
        broker.place_stop_order.assert_not_called()

    def test_survives_restart_from_disk(self):
        with tempfile.NamedTemporaryFile(suffix='.json', delete=False) as tmp:
            path = tmp.name
        frozen = _intraday_open_trade()
        frozen['broker_actions_disabled'] = True
        frozen['broker_actions_disabled_reason'] = 'instruments_stopped_trading'
        frozen['expiry_settlement_pending'] = True
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(frozen, f)
        broker = MagicMock()
        prices = MagicMock()
        prices.kill_switch = False
        with patch('blocks.stop.monitor.state_mod.load_state', return_value=frozen):
            monitor = StopMonitor(path, broker, prices, phases=[])
        self.assertTrue(monitor._broker_actions_blocked())
        broker.place_stop_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
