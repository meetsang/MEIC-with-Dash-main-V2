"""Tests for Skyline settlement math."""
from __future__ import annotations

import unittest

from strategies.skyline.settlement import pnl_points_to_dollars, structure_settlement_pnl


class TestSkylineSettlement(unittest.TestCase):
    def test_pnl_points_to_dollars(self):
        self.assertEqual(pnl_points_to_dollars(1.5, 1), 150.0)
        self.assertEqual(pnl_points_to_dollars(-3.65, 2), -730.0)

    def test_itm_call_butterfly_pnl(self):
        struct = {
            'net_debit': 2.0,
            'legs': [
                {'symbol': '.SPXW260730C6400', 'action': 'BUY_TO_OPEN', 'qty': 1},
                {'symbol': '.SPXW260730C6425', 'action': 'SELL_TO_OPEN', 'qty': 2},
                {'symbol': '.SPXW260730C6450', 'action': 'BUY_TO_OPEN', 'qty': 1},
            ],
        }
        pnl = structure_settlement_pnl(struct, 6425.0)
        self.assertIsInstance(pnl, float)


if __name__ == '__main__':
    unittest.main()
