"""Tests for debit/credit breach math."""
from __future__ import annotations

import unittest

from blocks.stop.breach import profit_target_hit, spread_breach_triggered, spread_mark_price


class TestBreachMath(unittest.TestCase):
    def test_credit_breach(self):
        self.assertTrue(spread_breach_triggered(3.0, 2.5, spread_type='credit'))
        self.assertFalse(spread_breach_triggered(2.0, 2.5, spread_type='credit'))

    def test_debit_breach(self):
        self.assertTrue(spread_breach_triggered(1.0, 1.5, spread_type='debit'))
        self.assertFalse(spread_breach_triggered(2.0, 1.5, spread_type='debit'))

    def test_credit_profit_target(self):
        self.assertTrue(profit_target_hit(0.5, 1.0, 0.5, spread_type='credit'))
        self.assertFalse(profit_target_hit(0.9, 1.0, 0.5, spread_type='credit'))

    def test_debit_profit_target(self):
        self.assertTrue(profit_target_hit(2.0, 1.0, 0.5, spread_type='debit'))
        self.assertFalse(profit_target_hit(1.2, 1.0, 0.5, spread_type='debit'))

    def test_debit_mark_price(self):
        self.assertEqual(spread_mark_price(1.0, 2.0, spread_type='debit'), 1.0)


if __name__ == '__main__':
    unittest.main()
