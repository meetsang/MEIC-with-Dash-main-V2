"""Verify TastyTrade debit/credit signing for all production order paths."""
import unittest
from decimal import Decimal

from brokers.tastytrade_broker import _signed_order_price


class TestBrokerOrderPaths(unittest.TestCase):
    """Maps actions used in production code to expected price signs."""

    def test_spread_open_credit(self):
        # place_spread_order: SELL short + BUY long → positive net credit
        self.assertGreater(_signed_order_price('SELL_TO_OPEN', 1.50), Decimal('0'))

    def test_stop_monitor_short_stop_debit(self):
        # stop_monitor.setup_*_stop → BUY_TO_CLOSE short leg
        self.assertLess(_signed_order_price('BUY_TO_CLOSE', 8.0), Decimal('0'))

    def test_stop_monitor_long_close_credit(self):
        # stop_monitor._close_long_leg → SELL_TO_CLOSE long leg
        self.assertGreater(_signed_order_price('SELL_TO_CLOSE', 0.05), Decimal('0'))

    def test_breach_limit_close_short_debit(self):
        # replace_with_limit_close → BUY_TO_CLOSE
        self.assertLess(_signed_order_price('BUY_TO_CLOSE', 4.25), Decimal('0'))

    def test_manual_adhoc_stop_debit(self):
        self.assertEqual(_signed_order_price('BUY_TO_CLOSE', 3.0), Decimal('-3.0'))


if __name__ == '__main__':
    unittest.main()
