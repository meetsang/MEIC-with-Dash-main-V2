"""Runtime session shutdown profiles (MEIC vs overnight futures)."""
from __future__ import annotations

import unittest
from datetime import datetime

from common.runtime_session import (
    FUTURES_OVERNIGHT,
    MEIC_SPX_0DTE,
    runtime_should_stop_for_session,
)
from meic0dte.app.utilities import crossed_market_close


class TestRuntimeSessionProfile(unittest.TestCase):
    def test_meic_post_close_start_stops(self):
        start = datetime(2026, 7, 8, 18, 17, 0)
        now = datetime(2026, 7, 8, 18, 20, 0)
        self.assertTrue(
            runtime_should_stop_for_session(start, now, profile=MEIC_SPX_0DTE)
        )

    def test_meic_next_calendar_day_stops(self):
        start = datetime(2026, 7, 8, 8, 30, 0)
        now = datetime(2026, 7, 9, 2, 0, 0)
        self.assertTrue(
            runtime_should_stop_for_session(start, now, profile=MEIC_SPX_0DTE)
        )

    def test_meic_same_day_before_close_does_not_stop(self):
        start = datetime(2026, 7, 8, 8, 30, 0)
        now = datetime(2026, 7, 8, 14, 59, 0)
        self.assertFalse(
            runtime_should_stop_for_session(start, now, profile=MEIC_SPX_0DTE)
        )

    def test_meic_same_day_at_close_stops(self):
        start = datetime(2026, 7, 8, 8, 30, 0)
        now = datetime(2026, 7, 8, 15, 0, 1)
        self.assertTrue(
            runtime_should_stop_for_session(start, now, profile=MEIC_SPX_0DTE)
        )

    def test_futures_post_close_start_does_not_stop(self):
        start = datetime(2026, 7, 8, 18, 0, 0)
        now = datetime(2026, 7, 8, 18, 30, 0)
        self.assertFalse(
            runtime_should_stop_for_session(start, now, profile=FUTURES_OVERNIGHT)
        )

    def test_futures_next_day_early_morning_does_not_stop(self):
        start = datetime(2026, 7, 8, 18, 0, 0)
        now = datetime(2026, 7, 9, 2, 0, 0)
        self.assertFalse(
            runtime_should_stop_for_session(start, now, profile=FUTURES_OVERNIGHT)
        )


class TestCrossedMarketCloseLegacy(unittest.TestCase):
    """Legacy time-only helper — not used for MEIC launcher shutdown."""

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
