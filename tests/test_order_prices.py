"""Unit tests for TastyTrade order price signing."""
import unittest
from decimal import Decimal

from brokers.tastytrade_broker import _signed_order_price


class TestOrderPriceSign(unittest.TestCase):
    def test_buy_to_close_is_debit(self):
        self.assertEqual(_signed_order_price('BUY_TO_CLOSE', 3.10), Decimal('-3.1'))

    def test_buy_to_close_rounds_invalid_dime(self):
        self.assertEqual(_signed_order_price('BUY_TO_CLOSE', 4.45), Decimal('-4.5'))

    def test_sell_to_close_is_credit(self):
        self.assertEqual(_signed_order_price('SELL_TO_CLOSE', 0.05), Decimal('0.05'))

    def test_sell_to_open_spread_credit(self):
        self.assertEqual(_signed_order_price('SELL_TO_OPEN', 1.50), Decimal('1.5'))

    def test_buy_to_open_is_debit(self):
        self.assertEqual(_signed_order_price('BUY_TO_OPEN', 2.00), Decimal('-2.0'))


if __name__ == '__main__':
    unittest.main()
