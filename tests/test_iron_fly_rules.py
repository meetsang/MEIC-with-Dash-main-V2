"""Unit tests for Dale Perryman's NFH Iron Fly Strategy rules."""
from __future__ import annotations

import unittest
from datetime import date, time

from strategies.iron_fly.rules import (
    DEFAULT_RULES,
    NfhRules,
    atm_center,
    build_closing_legs,
    build_fly_legs,
    calculate_bbe_amount,
    check_rapid_move,
    evaluate_add_trigger,
    first_fly_due,
    intrinsic,
    is_last_weekday_of_month,
    is_third_wednesday,
    is_tight_mode,
    next_center,
    pick_widths,
    price_fly,
    should_cut,
    skip_reason,
    straddle,
    tier_index,
    tier_params,
)


class TestIronFlyRules(unittest.TestCase):
    def test_atm_center(self):
        self.assertEqual(atm_center(5832.4), 5830.0)
        self.assertEqual(atm_center(5832.6), 5835.0)
        self.assertEqual(atm_center(5830.0), 5830.0)
        self.assertEqual(atm_center(5835.0), 5835.0)

    def test_skip_reasons(self):
        # 3rd Wednesday: 2026-10-21 (Wed, day 21)
        d_3rd_wed = date(2026, 10, 21)
        self.assertTrue(is_third_wednesday(d_3rd_wed))
        self.assertEqual(skip_reason(d_3rd_wed), 'third_wednesday')

        # 2nd Wednesday: 2026-10-14 (Wed, day 14)
        d_2nd_wed = date(2026, 10, 14)
        self.assertFalse(is_third_wednesday(d_2nd_wed))

        # Month's last weekday: 2026-10-30 (Friday, Oct 31 is Sat)
        d_last_wkday = date(2026, 10, 30)
        self.assertTrue(is_last_weekday_of_month(d_last_wkday))
        self.assertEqual(skip_reason(d_last_wkday), 'last_weekday_of_month')

        # Normal weekday: 2026-10-15
        self.assertFalse(is_last_weekday_of_month(date(2026, 10, 15)))

        # FOMC day
        fomc = ['2026-10-28', '2026-12-09']
        self.assertEqual(skip_reason(date(2026, 10, 28), fomc_days=fomc), 'fomc_decision_day')
        self.assertIsNone(skip_reason(date(2026, 10, 27), fomc_days=fomc))

        # Holiday & half-day
        self.assertEqual(skip_reason(date(2026, 10, 15), is_holiday=True), 'market_holiday')
        self.assertEqual(skip_reason(date(2026, 10, 15), is_half_day=True), 'half_day')

    def test_straddle(self):
        chain = {
            ('C', 5800.0): (15.50, 0.50, 15.40, 15.60),
            ('P', 5800.0): (14.25, -0.50, 14.15, 14.35),
        }
        tot, c_mid, p_mid = straddle(chain, 5800.0)
        self.assertEqual(tot, 29.75)
        self.assertEqual(c_mid, 15.50)
        self.assertEqual(p_mid, 14.25)

        # Missing quote
        self.assertEqual(straddle(chain, 5805.0), (None, None, None))

    def test_first_fly_due(self):
        # Before 10:35 -> False
        due, reason = first_fly_due(time(10, 30), has_prior_flies=False)
        self.assertFalse(due)
        self.assertEqual(reason, 'before_1035')

        # At/after 10:35 -> True
        due, reason = first_fly_due(time(10, 35), has_prior_flies=False)
        self.assertTrue(due)
        self.assertEqual(reason, 'first_fly_due')

        # After 15:50 cutoff -> False
        due, reason = first_fly_due(time(15, 51), has_prior_flies=False)
        self.assertFalse(due)
        self.assertEqual(reason, 'after_1550')

        # Already has flies -> False
        due, reason = first_fly_due(time(10, 45), has_prior_flies=True)
        self.assertFalse(due)
        self.assertEqual(reason, 'already_have_flies_today')

        # Mid price filter checks
        due, reason = first_fly_due(time(10, 35), has_prior_flies=False, initial_mid_credit=2.5)
        self.assertFalse(due)
        self.assertIn('below_min', reason)

        due, reason = first_fly_due(time(10, 35), has_prior_flies=False, initial_mid_credit=55.0)
        self.assertFalse(due)
        self.assertIn('above_max', reason)

        due, reason = first_fly_due(time(10, 35), has_prior_flies=False, initial_mid_credit=18.5)
        self.assertTrue(due)

    def test_is_tight_mode(self):
        rules = DEFAULT_RULES
        # Before 2:00 PM -> False
        self.assertFalse(is_tight_mode(time(13, 59), mid_50w=5.0, rules=rules))

        # At 2:00 PM with mid_50w < 10.0 -> True
        self.assertTrue(is_tight_mode(time(14, 0), mid_50w=8.5, rules=rules))

        # At 2:00 PM with mid_50w >= 10.0 -> False
        self.assertFalse(is_tight_mode(time(14, 0), mid_50w=12.0, rules=rules))

        # At 3:30 PM (late tight threshold) -> True unconditionally
        self.assertTrue(is_tight_mode(time(15, 30), mid_50w=15.0, rules=rules))
        self.assertTrue(is_tight_mode(time(15, 45), mid_50w=None, rules=rules))

    def test_evaluate_add_trigger(self):
        rules = DEFAULT_RULES
        # Scenario 1: High credit (> 20.0), threshold = 15.0, offset = 20.0
        # Spot at 5814 from center 5800 -> ITM 14 <= 15 -> False
        trig, scen, th, off, dist = evaluate_add_trigger(5814.0, 5800.0, 22.0, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'high_credit')
        self.assertEqual(th, 15.0)
        self.assertEqual(off, 20.0)

        # Spot at 5816 from center 5800 -> ITM 16 > 15 -> True
        trig, scen, th, off, dist = evaluate_add_trigger(5816.0, 5800.0, 22.0, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'high_credit')
        self.assertEqual(off, 20.0)

        # Scenario 2: Mid credit (15.0 - 20.0), threshold = 10.0, offset = 15.0
        # Spot at 5809 -> ITM 9 <= 10 -> False
        trig, scen, th, off, dist = evaluate_add_trigger(5809.0, 5800.0, 18.0, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'mid_credit')
        self.assertEqual(th, 10.0)
        self.assertEqual(off, 15.0)

        # Spot at 5811 -> ITM 11 > 10 -> True
        trig, scen, th, off, dist = evaluate_add_trigger(5811.0, 5800.0, 18.0, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'mid_credit')
        self.assertEqual(off, 15.0)

        # Scenario 3: Low credit (< 15.0), threshold = 7.5, offset = 10.0
        # Spot at 5807 -> ITM 7 <= 7.5 -> False
        trig, scen, th, off, dist = evaluate_add_trigger(5807.0, 5800.0, 12.0, time(11, 0), rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'low_credit')
        self.assertEqual(th, 7.5)
        self.assertEqual(off, 10.0)

        # Spot at 5808 -> ITM 8 > 7.5 -> True
        trig, scen, th, off, dist = evaluate_add_trigger(5808.0, 5800.0, 12.0, time(11, 0), rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'low_credit')
        self.assertEqual(off, 10.0)

        # Scenario 4: Tight mode (after 2:00 PM with mid_50w < 10)
        # Threshold = 2.5, offset = 5.0
        # Spot at 5802.0 -> ITM 2 <= 2.5 -> False
        trig, scen, th, off, dist = evaluate_add_trigger(5802.0, 5800.0, 12.0, time(14, 15), mid_50w=8.0, rules=rules)
        self.assertFalse(trig)
        self.assertEqual(scen, 'tight')
        self.assertEqual(th, 2.5)
        self.assertEqual(off, 5.0)

        # Spot at 5803.0 -> ITM 3 > 2.5 -> True
        trig, scen, th, off, dist = evaluate_add_trigger(5803.0, 5800.0, 12.0, time(14, 15), mid_50w=8.0, rules=rules)
        self.assertTrue(trig)
        self.assertEqual(scen, 'tight')
        self.assertEqual(off, 5.0)

    def test_next_center_and_clash(self):
        open_centers = [5800.0]

        # Spot moved up to 5811 with offset 10 -> candidate is 5810
        self.assertEqual(next_center(5811.0, 5800.0, 10.0, open_centers), 5810.0)

        # Spot moved down to 5789 with offset 10 -> candidate is 5790
        self.assertEqual(next_center(5789.0, 5800.0, 10.0, open_centers), 5790.0)

        # Candidate already open -> returns None
        open_centers.append(5810.0)
        self.assertIsNone(next_center(5811.0, 5800.0, 10.0, open_centers))

        # Clash rule: do not open a strike that the book is long
        long_map = {('C', 5820.0): 1}
        self.assertIsNone(next_center(5818.0, 5800.0, 20.0, [5800.0], long_legs_map=long_map))

    def test_pick_widths(self):
        # Initial fly: must be 50 wide
        chain = {}
        cw, pw = pick_widths(chain, 5800.0, is_initial=True)
        self.assertEqual(cw, 50)
        self.assertEqual(pw, 50)

        # Additional fly: target 0.40 bounded between 20 and 50
        chain = {
            ('C', 5820.0): (0.80, 0, 0.75, 0.85),
            ('C', 5830.0): (0.35, 0, 0.30, 0.40),
            ('P', 5780.0): (0.90, 0, 0.85, 0.95),
            ('P', 5770.0): (0.38, 0, 0.35, 0.40),
        }
        cw, pw = pick_widths(chain, 5800.0, is_initial=False, target_price=0.40, min_width=20, max_width=50)
        self.assertEqual(pw, 30)  # 5770 is 30 wide with mid 0.38 <= 0.40
        self.assertEqual(cw, 30)  # symmetric matching 30 wide with mid 0.35 <= 0.40

    def test_calculate_bbe_amount(self):
        rules = DEFAULT_RULES
        # Default: 0.01
        self.assertEqual(calculate_bbe_amount(18.0, time(11, 0), rules), 0.01)

        # With PABBE enabled
        pabbe_rules = NfhRules(pabbe_close=True)
        self.assertEqual(calculate_bbe_amount(22.0, time(11, 0), pabbe_rules), 2.0)
        self.assertEqual(calculate_bbe_amount(18.0, time(11, 0), pabbe_rules), 1.0)
        self.assertEqual(calculate_bbe_amount(12.0, time(11, 0), pabbe_rules), 0.01)

        # With Late BBE adjustment at >= 15:30
        late_rules = NfhRules(late_bbe_adjustment=True)
        self.assertEqual(calculate_bbe_amount(18.0, time(15, 30), late_rules), 1.50)

        # With EOD BBE adjustment at >= 15:50
        eod_rules = NfhRules(eod_bbe_adjustment=True)
        self.assertEqual(calculate_bbe_amount(18.0, time(15, 50), eod_rules), 2.50)

    def test_should_cut(self):
        be_low, be_high = 5780.0, 5820.0
        bbe = 0.01

        # Inside breakeven tent: safe
        self.assertFalse(should_cut(5800.0, be_low, be_high, beyond_be_amount=bbe)[0])
        self.assertFalse(should_cut(5780.0, be_low, be_high, beyond_be_amount=bbe)[0])
        self.assertFalse(should_cut(5820.0, be_low, be_high, beyond_be_amount=bbe)[0])

        # Beyond low breach: spot <= 5780.0 - 0.01 = 5779.99
        cut, reason = should_cut(5779.99, be_low, be_high, beyond_be_amount=bbe)
        self.assertTrue(cut)
        self.assertEqual(reason, 'be_low_breach')

        # Beyond high breach: spot >= 5820.0 + 0.01 = 5820.01
        cut, reason = should_cut(5820.01, be_low, be_high, beyond_be_amount=bbe)
        self.assertTrue(cut)
        self.assertEqual(reason, 'be_high_breach')

        # RoR guard: if return on risk < -90%, do not cut!
        cut, reason = should_cut(5775.0, be_low, be_high, beyond_be_amount=bbe, current_ror=-0.95, min_close_ror=-0.90)
        self.assertFalse(cut)
        self.assertEqual(reason, 'ror_below_min_close')

    def test_check_rapid_move(self):
        now_ts = 1000.0
        # 1-minute 25 pt move
        spot_trail = [(now_ts - 50.0, 5800.0), (now_ts - 10.0, 5826.0)]
        is_rapid, reason = check_rapid_move(spot_trail, now_ts)
        self.assertTrue(is_rapid)
        self.assertIn('rapid_move_1m', reason)

        # Normal small move
        spot_trail_normal = [(now_ts - 50.0, 5800.0), (now_ts - 10.0, 5805.0)]
        is_rapid, reason = check_rapid_move(spot_trail_normal, now_ts)
        self.assertFalse(is_rapid)

    def test_intrinsic_settlement(self):
        self.assertEqual(intrinsic(5800.0, 5800.0, 50, 50), 0.0)
        self.assertEqual(intrinsic(5815.0, 5800.0, 50, 50), 15.0)
        self.assertEqual(intrinsic(5860.0, 5800.0, 50, 50), 50.0)
        self.assertEqual(intrinsic(5740.0, 5800.0, 50, 50), 50.0)

    def test_build_legs(self):
        open_legs = build_fly_legs('261002', 5800.0, 50, 50, qty=1, broker_format='tastytrade')
        self.assertEqual(len(open_legs), 4)
        self.assertEqual(open_legs[0].symbol, '.SPXW261002C5800')
        self.assertEqual(open_legs[1].symbol, '.SPXW261002P5800')
        self.assertEqual(open_legs[2].symbol, '.SPXW261002C5850')
        self.assertEqual(open_legs[3].symbol, '.SPXW261002P5750')

        close_legs = build_closing_legs('261002', 5800.0, 50, 50, qty=1, broker_format='tastytrade')
        self.assertEqual(close_legs[0].action, 'BUY_TO_CLOSE')
        self.assertEqual(close_legs[1].action, 'BUY_TO_CLOSE')
        self.assertEqual(close_legs[2].action, 'SELL_TO_CLOSE')
        self.assertEqual(close_legs[3].action, 'SELL_TO_CLOSE')


if __name__ == '__main__':
    unittest.main()
