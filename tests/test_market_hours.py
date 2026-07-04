"""0DTE market close gates (3:00 PM Central)."""
from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

from blocks.stop.fill_sync import stop_is_current
from common.market_hours import (
    is_after_market_close_ct,
    session_row_is_0dte,
    session_row_past_0dte_close,
    trade_expiry_on_or_before_today,
    trade_past_0dte_close,
)
from common import trades_layout


class TestMarketHours(unittest.TestCase):
    def test_after_market_close_at_three_pm(self):
        now = datetime(2026, 6, 26, 15, 0, 1)
        self.assertTrue(is_after_market_close_ct(now))
        self.assertFalse(is_after_market_close_ct(datetime(2026, 6, 26, 14, 59, 59)))

    def test_trade_0dte_from_symbol(self):
        state = {
            'short_leg': {'symbol': '.SPXW260626P7320'},
            'long_leg': {'symbol': '.SPXW260626P7295'},
        }
        with patch('meic0dte.app.utilities.central_date', return_value=date(2026, 6, 26)):
            self.assertTrue(trade_expiry_on_or_before_today(state, 'x.json'))
            self.assertTrue(
                trade_past_0dte_close(state, 'x.json', now=datetime(2026, 6, 26, 15, 1))
            )
            self.assertFalse(
                trade_past_0dte_close(state, 'x.json', now=datetime(2026, 6, 26, 14, 30))
            )

    def test_future_expiry_not_frozen(self):
        state = {
            'short_leg': {'symbol': '.SPXW260703P7320'},
            'long_leg': {'symbol': '.SPXW260703P7295'},
        }
        with patch('meic0dte.app.utilities.central_date', return_value=date(2026, 6, 26)):
            self.assertFalse(trade_expiry_on_or_before_today(state))
            self.assertFalse(
                trade_past_0dte_close(state, now=datetime(2026, 6, 26, 15, 30))
            )

    def test_meic_session_row_is_0dte(self):
        row = MagicMock(expiry='')
        self.assertTrue(session_row_is_0dte(row, strategy=trades_layout.STRATEGY_MEIC))

    def test_manual_session_row_past_close(self):
        row = MagicMock(expiry='260626')
        with patch('meic0dte.app.utilities.central_date', return_value=date(2026, 6, 26)):
            self.assertTrue(
                session_row_past_0dte_close(
                    row,
                    strategy=trades_layout.STRATEGY_MANUAL,
                    now=datetime(2026, 6, 26, 15, 5),
                )
            )

    def test_stop_is_current_rejects_expired(self):
        state = {
            'filled_quantity': 1,
            'stop_quantity': 1,
            'active_stop': {'order_id': '1', 'status': 'expired'},
        }
        self.assertFalse(stop_is_current(state))


if __name__ == '__main__':
    unittest.main()
