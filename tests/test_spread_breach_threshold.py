"""Software breach threshold uses 2× net credit on spread mid, not 2× short leg."""
from __future__ import annotations

import unittest

from blocks.stop.breach import spread_breach_triggered, spread_mark_price
from blocks.stop.stop_math import spread_breach_threshold


class TestSpreadBreachThreshold(unittest.TestCase):
    def test_01_45_pcs_threshold(self):
        """01-45 7340/7315 PCS — breach at spread ~3.80, not ~4.75."""
        state = {
            'entry': {'net_credit': 1.8, 'two_x_net_credit': 3.6, 'side': 'P'},
            'short_leg': {'fill_price': 2.27, 'two_x_short': 4.55},
            'stop_multiplier': 2,
        }
        threshold = spread_breach_threshold(state)
        self.assertEqual(threshold, 3.8)

        # Short rising but exchange stop (4.3) not hit — spread already at risk limit
        spread = spread_mark_price(4.15, 0.35)
        self.assertEqual(spread, 3.8)
        self.assertTrue(spread_breach_triggered(spread, threshold))

        # Old short×2+0.20 threshold (4.75) would miss this
        self.assertFalse(spread_breach_triggered(spread, 4.75))

    def test_12_00_pcs_threshold(self):
        """12-00 7340 PCS — software breach before short leg hits 6.3 exchange stop."""
        state = {
            'entry': {'net_credit': 1.9, 'two_x_net_credit': 3.8, 'side': 'P'},
            'short_leg': {'fill_price': 3.27, 'two_x_short': 6.55},
            'stop_multiplier': 2,
        }
        threshold = spread_breach_threshold(state)
        self.assertEqual(threshold, 4.0)

        spread = spread_mark_price(5.2, 1.15)
        self.assertEqual(spread, 4.05)
        self.assertTrue(spread_breach_triggered(spread, threshold))

        # Would not breach old short×2 threshold (6.75) while already past 2× credit
        self.assertFalse(spread_breach_triggered(spread, 6.75))


if __name__ == '__main__':
    unittest.main()
