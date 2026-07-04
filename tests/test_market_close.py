"""Market close session logic."""
from __future__ import annotations

import unittest
from datetime import datetime, time

from meic0dte.app.utilities import crossed_market_close


class TestCrossedMarketClose(unittest.TestCase):
    def test_after_hours_start_does_not_trigger(self):
        start = datetime(2026, 6, 24, 21, 59, 0)
        now = datetime(2026, 6, 24, 22, 0, 0)
        self.assertFalse(crossed_market_close(start, now))

    def test_morning_session_triggers_at_close(self):
        start = datetime(2026, 6, 24, 8, 30, 0)
        now = datetime(2026, 6, 24, 15, 0, 1)
        self.assertTrue(crossed_market_close(start, now))

    def test_before_close_does_not_trigger(self):
        start = datetime(2026, 6, 24, 8, 30, 0)
        now = datetime(2026, 6, 24, 14, 59, 0)
        self.assertFalse(crossed_market_close(start, now))


if __name__ == '__main__':
    unittest.main()
