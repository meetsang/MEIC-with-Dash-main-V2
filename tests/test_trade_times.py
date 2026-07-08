"""Dashboard trade time helpers."""
from __future__ import annotations

import unittest

from dashboard.trade_times import trade_entry_time_iso, trade_exit_time_iso


class TestTradeTimes(unittest.TestCase):
    def test_entry_from_iso(self):
        trade = {'entry': {'timestamp': '2026-07-07T12:30:15-05:00'}}
        self.assertEqual(trade_entry_time_iso(trade), '2026-07-07T12:30:15-05:00')

    def test_exit_prefers_close_timestamp(self):
        trade = {
            'close': {'timestamp': '2026-07-07T13:50:02-05:00'},
            'short_closed_at': 1_000_000.0,
        }
        self.assertEqual(trade_exit_time_iso(trade), '2026-07-07T13:50:02-05:00')

    def test_exit_falls_back_to_short_closed_at(self):
        trade = {'short_closed_at': 1_750_000_000.0}
        iso = trade_exit_time_iso(trade)
        self.assertIn('T', iso)


if __name__ == '__main__':
    unittest.main()
