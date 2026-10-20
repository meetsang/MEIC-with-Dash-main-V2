"""Unit tests for XSP Iron Fly pure rules."""
from __future__ import annotations

import unittest
from datetime import time

from strategies.iron_fly_xsp.rules import (
    DEFAULT_RULES_XSP,
    NfhRulesXsp,
    atm_center_spx,
    calculate_bbe_amount_xsp,
    evaluate_add_trigger_xsp,
    get_xsp_centers,
    intrinsic_xsp,
    is_tight_mode_xsp,
    next_spx_center_xsp,
    pick_widths_xsp,
    price_fly_xsp,
    should_cut_xsp,
    tier_index_xsp,
    tier_params_xsp,
)


class TestIronFlyXspRules(unittest.TestCase):
    def test_get_xsp_centers_fractional(self):
        # SPX 7755 -> 775.5 -> 1 fly at 775 and 1 fly at 776
        ca, cb = get_xsp_centers(7755)
        self.assertEqual(ca, 775)
        self.assertEqual(cb, 776)
        self.assertEqual((ca + cb) / 2.0, 775.5)

    def test_get_xsp_centers_integer(self):
        # SPX 7750 -> 775.0 -> 2 flies at 775
        ca, cb = get_xsp_centers(7750)
        self.assertEqual(ca, 775)
        self.assertEqual(cb, 775)
        self.assertEqual((ca + cb) / 2.0, 775.0)

    def test_get_xsp_centers_various(self):
        ca, cb = get_xsp_centers(5765)
        self.assertEqual((ca, cb), (576, 577))
        ca, cb = get_xsp_centers(5760)
        self.assertEqual((ca, cb), (576, 576))

    def test_evaluate_add_trigger_xsp(self):
        rules = DEFAULT_RULES_XSP

        # 1. High credit (> 2.00): threshold = 1.50, offset = 2.00
        # Spot at 581.40 from center 580.00 -> ITM 1.40 <= 1.50 -> False
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(581.40, 580.00, 2.20, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'high_credit')
        self.assertEqual(th, 1.50)
        self.assertEqual(off, 2.00)

        # Spot at 581.60 from center 580.00 -> ITM 1.60 > 1.50 -> True
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(581.60, 580.00, 2.20, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'high_credit')
        self.assertEqual(off, 2.00)

        # 2. Mid credit (1.50 - 2.00): threshold = 1.00, offset = 1.50
        # Spot at 580.90 -> ITM 0.90 <= 1.00 -> False
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(580.90, 580.00, 1.80, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'mid_credit')
        self.assertEqual(th, 1.00)
        self.assertEqual(off, 1.50)

        # Spot at 581.10 -> ITM 1.10 > 1.00 -> True
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(581.10, 580.00, 1.80, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'mid_credit')
        self.assertEqual(off, 1.50)

        # 3. Low credit (< 1.50): threshold = 0.75, offset = 1.00
        # Spot at 580.70 -> ITM 0.70 <= 0.75 -> False
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(580.70, 580.00, 1.20, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'low_credit')
        self.assertEqual(th, 0.75)
        self.assertEqual(off, 1.00)

        # Spot at 580.80 -> ITM 0.80 > 0.75 -> True
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(580.80, 580.00, 1.20, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'low_credit')
        self.assertEqual(off, 1.00)

        # 4. Tight mode: threshold = 0.25, offset = 0.50
        # Spot at 580.20 -> ITM 0.20 <= 0.25 -> False
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(580.20, 580.00, 1.20, time(14, 15), mid_5w=0.80, rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'tight')
        self.assertEqual(th, 0.25)
        self.assertEqual(off, 0.50)

        # Spot at 580.30 -> ITM 0.30 > 0.25 -> True
        trig, scen, th, off, dist = evaluate_add_trigger_xsp(580.30, 580.00, 1.20, time(14, 15), mid_5w=0.80, rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'tight')
        self.assertEqual(off, 0.50)

    def test_pick_widths_xsp(self):
        # Initial fly: must be 5 wide
        cw, pw = pick_widths_xsp(50, 50, 775, 776, is_initial=True)
        self.assertEqual(cw, 5)
        self.assertEqual(pw, 5)

        # Additional fly: 30 pt SPX width -> 3 pt XSP width
        cw, pw = pick_widths_xsp(30, 30, 775, 776, is_initial=False)
        self.assertEqual(cw, 3)
        self.assertEqual(pw, 3)

        # Clash avoidance: if 778 is an existing short, step out to 4
        cw2, pw2 = pick_widths_xsp(30, 30, 775, 776, existing_short_strikes={778})
        self.assertEqual(cw2, 4)
        self.assertEqual(pw2, 3)

    def test_price_fly_xsp(self):
        chain = {
            ('C', 775): {'bid': 3.10, 'ask': 3.30, 'mid': 3.20},
            ('P', 775): {'bid': 3.00, 'ask': 3.20, 'mid': 3.10},
            ('C', 778): {'bid': 1.80, 'ask': 2.00, 'mid': 1.90},
            ('P', 772): {'bid': 1.70, 'ask': 1.90, 'mid': 1.80},
        }
        pricing = price_fly_xsp(775, 3, 3, chain)
        self.assertIsNotNone(pricing)
        nat, mid = pricing
        # Short Bids (3.10 + 3.00) - Long Asks (2.00 + 1.90) = 6.10 - 3.90 = 2.20
        self.assertEqual(nat, 2.20)
        # Short Mids (3.20 + 3.10) - Long Mids (1.90 + 1.80) = 6.30 - 3.70 = 2.60
        self.assertEqual(mid, 2.60)

    def test_should_cut_xsp(self):
        # Fly BE: low=773.15, high=777.85, bbe=0.01
        # Inside range -> safe
        cut, _ = should_cut_xsp(775.50, 773.15, 777.85, beyond_be_amount=0.01)
        self.assertFalse(cut)

        # Below be_low - 0.01 (773.14) -> cut
        cut, msg = should_cut_xsp(773.14, 773.15, 777.85, beyond_be_amount=0.01)
        self.assertTrue(cut)

        # Above be_high + 0.01 (777.86) -> cut
        cut, msg = should_cut_xsp(777.86, 773.15, 777.85, beyond_be_amount=0.01)
        self.assertTrue(cut)

        # RoR guard: if return on risk < -90%, do not cut!
        cut, msg = should_cut_xsp(770.0, 773.15, 777.85, beyond_be_amount=0.01, current_ror=-0.95, min_close_ror=-0.90)
        self.assertFalse(cut)
        self.assertEqual(msg, 'ror_below_min_close')

    def test_calculate_bbe_amount_xsp(self):
        rules = DEFAULT_RULES_XSP
        self.assertEqual(calculate_bbe_amount_xsp(1.80, time(11, 0), rules), 0.01)

        pabbe_rules = NfhRulesXsp(pabbe_close=True)
        self.assertEqual(calculate_bbe_amount_xsp(2.20, time(11, 0), pabbe_rules), 0.20)
        self.assertEqual(calculate_bbe_amount_xsp(1.80, time(11, 0), pabbe_rules), 0.10)
        self.assertEqual(calculate_bbe_amount_xsp(1.20, time(11, 0), pabbe_rules), 0.01)

    def test_intrinsic_xsp(self):
        self.assertEqual(intrinsic_xsp(775.0, 775, 778, 772), 0.0)
        self.assertEqual(intrinsic_xsp(776.0, 775, 778, 772), 1.0)
        self.assertEqual(intrinsic_xsp(779.0, 775, 778, 772), 3.0)


if __name__ == '__main__':
    unittest.main()
