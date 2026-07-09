"""Strategy-specific broker-action window gates."""
from __future__ import annotations

import unittest
from datetime import datetime

from common.broker_action_window import (
    FUTURES_OVERNIGHT_ACTIONS,
    MEIC_SPX_OPTIONS_RTH_ACTIONS,
    broker_actions_allowed_for_trade,
)


def _spx_state(expiry_yymmdd: str) -> dict:
    return {
        'status': 'open',
        'quantity': 1,
        'filled_quantity': 1,
        'stop_quantity': 0,
        'short_leg': {'symbol': f'.SPXW{expiry_yymmdd}P7435', 'strike': 7435, 'fill_price': 2.0},
        'long_leg': {'symbol': f'.SPXW{expiry_yymmdd}P7410', 'strike': 7410, 'fill_price': 0.8},
        'entry': {'side': 'P'},
    }


class TestBrokerActionWindow(unittest.TestCase):
    def test_expired_0dte_at_midnight_is_expired_option(self):
        state = _spx_state('260708')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 0, 1),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'expired_option')

    def test_1dte_overnight_outside_window(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 2, 0),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'outside_meic_spx_broker_action_window')

    def test_1dte_before_window_open(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 8, 29),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'outside_meic_spx_broker_action_window')

    def test_1dte_after_window_open(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 8, 31),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allowed')

    def test_intraday_allowed(self):
        state = _spx_state('260709')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 10, 0),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allowed')

    def test_same_day_after_close_expired_before_window(self):
        state = _spx_state('260709')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 15, 30),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'expired_option')

    def test_1dte_after_close_outside_window(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 15, 30),
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'outside_meic_spx_broker_action_window')

    def test_futures_profile_overnight_allowed(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 2, 0),
            profile=FUTURES_OVERNIGHT_ACTIONS,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allowed')

    def test_window_start_inclusive(self):
        state = _spx_state('260710')
        allowed, reason = broker_actions_allowed_for_trade(
            state,
            now=datetime(2026, 7, 9, 8, 30),
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, 'allowed')


if __name__ == '__main__':
    unittest.main()
