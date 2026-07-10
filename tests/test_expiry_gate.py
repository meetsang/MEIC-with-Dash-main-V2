"""Expiry gate — settle or freeze expired trades before broker actions."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from blocks.stop.expiry_gate import try_settle_or_freeze_trade
from blocks.stop.monitor import StopMonitor


def _open_trade_state() -> dict:
    return {
        'status': 'open',
        'quantity': 1,
        'filled_quantity': 1,
        'stop_quantity': 0,
        'spread_type': 'credit',
        'entry': {'side': 'P', 'net_credit': 1.4, 'strategy': 'MEIC_IC'},
        'short_leg': {
            'symbol': '.SPXW260708P7320',
            'strike': 7320,
            'fill_price': 2.0,
        },
        'long_leg': {
            'symbol': '.SPXW260708P7295',
            'strike': 7295,
            'fill_price': 0.5,
        },
        'phases': {},
        'recovery': {
            'module_start_count': 0,
            'last_heartbeat': '2026-07-08T10:00:00-05:00',
            'state_loaded_from_disk': False,
        },
    }


class TestExpiryGate(unittest.TestCase):
    def test_settlement_closes_trade(self):
        state = _open_trade_state()
        now = datetime(2026, 7, 9, 0, 30)
        with patch(
            'blocks.stop.expiry_gate.get_spx_settlement_close',
            return_value=7471.32,
        ):
            outcome, state = try_settle_or_freeze_trade(
                state,
                path='trade_SPXW_260708_P.json',
                now=now,
            )
        self.assertEqual(outcome, 'settled')
        self.assertEqual(state['status'], 'closed')
        self.assertEqual(state['close_mechanism'], 'expiry_settlement')
        self.assertTrue(state['settled_at_expiry'])
        self.assertIn('pnl', state)
        self.assertIsNone(state.get('active_stop'))

    def test_missing_spx_freezes_without_closing(self):
        state = _open_trade_state()
        now = datetime(2026, 7, 9, 0, 30)
        with patch('blocks.stop.expiry_gate.ensure_spx_settlement_close', return_value=None):
            with patch('blocks.stop.expiry_gate.get_spx_settlement_close', return_value=None):
                outcome, state = try_settle_or_freeze_trade(
                    state,
                    path='trade.json',
                    now=now,
                )
        self.assertEqual(outcome, 'frozen')
        self.assertEqual(state['status'], 'open')
        self.assertTrue(state['broker_actions_frozen'])
        self.assertTrue(state['expiry_settlement_pending'])
        self.assertEqual(state['broker_actions_disabled_reason'], 'expired_option')

    def test_expired_open_trade_does_not_place_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade_SPXW_260708_P.json')
            broker = MagicMock()
            prices = MagicMock()
            prices.get_spx.return_value = 7350.0
            prices.kill_switch = False
            state = _open_trade_state()
            with patch('blocks.stop.monitor.state_mod.load_state', return_value=state):
                monitor = StopMonitor(path, broker, prices, phases=[])
            now = datetime(2026, 7, 9, 0, 30)
            settled = dict(state)
            settled['status'] = 'closed'
            settled['close_mechanism'] = 'expiry_settlement'

            def _settle(st, **kwargs):
                return 'settled', settled

            with patch('blocks.stop.monitor.try_settle_or_freeze_trade', side_effect=_settle), \
                 patch('meic0dte.app.utilities.central_now', return_value=now):
                with patch('blocks.stop.monitor.state_mod.save_state'):
                    monitor._on_load()
            broker.place_stop_order.assert_not_called()
            self.assertEqual(monitor.state['status'], 'closed')


if __name__ == '__main__':
    unittest.main()
