"""Trade P&L scales with quantity."""
from __future__ import annotations

import unittest

from dashboard.server import _trade_pnl


class TestTradePnlQuantity(unittest.TestCase):
    def test_closed_pnl_scales_with_qty(self):
        pnl1, _, _, _ = _trade_pnl(
            net_credit=1.0,
            quantity=1,
            short_fill=2.0,
            long_fill=0.5,
            short_close=3.0,
            long_close=1.0,
            cur_short=3.0,
            cur_long=1.0,
            status='closed',
        )
        pnl2, _, _, _ = _trade_pnl(
            net_credit=1.0,
            quantity=2,
            short_fill=2.0,
            long_fill=0.5,
            short_close=3.0,
            long_close=1.0,
            cur_short=3.0,
            cur_long=1.0,
            status='closed',
        )
        # exit debit = 2.0; pnl = (1.0 - 2.0) * 100 * qty
        self.assertEqual(pnl1, -100.0)
        self.assertEqual(pnl2, -200.0)

    def test_live_pnl_scales_with_qty(self):
        pnl1, _, _, frozen = _trade_pnl(
            net_credit=1.2,
            quantity=1,
            short_fill=1.4,
            long_fill=0.2,
            short_close=None,
            long_close=None,
            cur_short=1.0,
            cur_long=0.2,
            status='open',
        )
        pnl2, _, _, _ = _trade_pnl(
            net_credit=1.2,
            quantity=3,
            short_fill=1.4,
            long_fill=0.2,
            short_close=None,
            long_close=None,
            cur_short=1.0,
            cur_long=0.2,
            status='open',
        )
        self.assertFalse(frozen)
        self.assertEqual(pnl1, 40.0)
        self.assertEqual(pnl2, 120.0)


if __name__ == '__main__':
    unittest.main()
