"""Unit tests for CBOE SPX option tick rounding."""
import unittest

from common.option_ticks import (
    SPX_OPTION_TICK_THRESHOLD,
    round_spx_option_price,
    spx_option_tick,
    step_down_spx_option_price,
)


class TestOptionTicks(unittest.TestCase):
    def test_tick_below_threshold(self):
        self.assertEqual(spx_option_tick(2.99), 0.05)
        self.assertEqual(spx_option_tick(0.15), 0.05)

    def test_tick_at_or_above_threshold(self):
        self.assertEqual(spx_option_tick(3.0), 0.10)
        self.assertEqual(spx_option_tick(4.45), 0.10)

    def test_round_nickel_premiums(self):
        self.assertEqual(round_spx_option_price(0.12), 0.10)
        self.assertEqual(round_spx_option_price(0.15), 0.15)
        self.assertEqual(round_spx_option_price(2.87), 2.85)

    def test_round_dime_premiums(self):
        self.assertEqual(round_spx_option_price(3.11), 3.10)
        self.assertEqual(round_spx_option_price(4.45), 4.50)

    def test_threshold_constant(self):
        self.assertEqual(SPX_OPTION_TICK_THRESHOLD, 3.0)

    def test_step_down_nickel_premium(self):
        self.assertEqual(step_down_spx_option_price(0.15), 0.10)
        self.assertEqual(step_down_spx_option_price(0.20), 0.15)

    def test_step_down_dime_premium(self):
        self.assertEqual(step_down_spx_option_price(3.10), 3.00)
        self.assertEqual(step_down_spx_option_price(4.50), 4.40)

    def test_step_down_floor(self):
        self.assertEqual(step_down_spx_option_price(0.05), 0.05)


if __name__ == '__main__':
    unittest.main()
