"""Tests for dashboard ghost JSON trade selection."""
from __future__ import annotations

import unittest

from common.trade_pick import pick_best_trade


def _trade(
    *,
    status: str,
    filled_quantity: int = 0,
    timestamp: str = '',
    short_strike: int = 7000,
) -> dict:
    return {
        'status': status,
        'filled_quantity': filled_quantity,
        'entry': {'timestamp': timestamp},
        'short_leg': {'strike': short_strike},
    }


class TestPickBestTrade(unittest.TestCase):
    def test_prefers_filled_over_ghost_pending(self):
        ghost = _trade(status='pending_fill', filled_quantity=0, timestamp='2026-06-25T14:00:00')
        live = _trade(status='open', filled_quantity=3, timestamp='2026-06-25T13:00:00', short_strike=7320)
        pick = pick_best_trade([ghost, live])
        self.assertEqual(pick['short_leg']['strike'], 7320)
        self.assertEqual(pick['status'], 'open')

    def test_prefers_open_over_pending_when_both_unfilled(self):
        older = _trade(status='pending_fill', timestamp='2026-06-25T14:00:00')
        newer = _trade(status='pending_fill', timestamp='2026-06-25T14:05:00')
        pick = pick_best_trade([older, newer])
        self.assertEqual(pick['entry']['timestamp'], '2026-06-25T14:05:00')

    def test_empty_returns_none(self):
        self.assertIsNone(pick_best_trade([]))


if __name__ == '__main__':
    unittest.main()
