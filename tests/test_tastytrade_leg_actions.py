"""TastyTrade SDK leg action string parsing (v12 human-readable actions)."""
import unittest
from types import SimpleNamespace

from brokers.tastytrade_broker import _normalize_leg_action, _order_result_from_placed_order


class TestTastytradeLegActions(unittest.TestCase):
    def test_normalize_human_readable_actions(self):
        self.assertEqual(_normalize_leg_action('Sell to Open'), 'SELL_TO_OPEN')
        self.assertEqual(_normalize_leg_action('Buy to Open'), 'BUY_TO_OPEN')
        self.assertEqual(_normalize_leg_action('Buy to Close'), 'BUY_TO_CLOSE')

    def test_order_result_extracts_leg_fills_from_sdk_actions(self):
        order = SimpleNamespace(
            id=477705364,
            status='Filled',
            size='1',
            price='1.4',
            legs=[
                SimpleNamespace(
                    action='Buy to Open',
                    quantity=1,
                    remaining_quantity='0',
                    fills=[
                        SimpleNamespace(fill_price='0.32', quantity='1'),
                    ],
                ),
                SimpleNamespace(
                    action='Sell to Open',
                    quantity=1,
                    remaining_quantity='0',
                    fills=[
                        SimpleNamespace(fill_price='1.72', quantity='1'),
                    ],
                ),
            ],
        )
        result = _order_result_from_placed_order(order)
        self.assertEqual(result.short_fill_price, 1.72)
        self.assertEqual(result.long_fill_price, 0.32)
        self.assertEqual(result.filled_price, 1.4)
        self.assertEqual(result.filled_quantity, 1)
        self.assertEqual(result.status, 'filled')

    def test_buy_to_close_uses_leg_fill_not_order_price(self):
        order = SimpleNamespace(
            id=479157208,
            status='Filled',
            size='1',
            price='-6.3',
            legs=[
                SimpleNamespace(
                    action='Buy to Close',
                    quantity=1,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='5.7', quantity='1')],
                ),
            ],
        )
        result = _order_result_from_placed_order(order)
        self.assertEqual(result.filled_price, 5.7)
        self.assertEqual(result.filled_quantity, 1)

    def test_sell_to_close_uses_leg_fill_not_limit(self):
        order = SimpleNamespace(
            id=479173772,
            status='Filled',
            size='1',
            price='3.1',
            legs=[
                SimpleNamespace(
                    action='Sell to Close',
                    quantity=1,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='3.3', quantity='1')],
                ),
            ],
        )
        result = _order_result_from_placed_order(order)
        self.assertEqual(result.filled_price, 3.3)
        self.assertEqual(result.filled_quantity, 1)

    def test_spread_close_extracts_both_leg_fills(self):
        """V3-4: BTC short + STC long on spread-close order."""
        order = SimpleNamespace(
            id=480934549,
            status='Filled',
            size='3',
            price='-0.20',
            legs=[
                SimpleNamespace(
                    action='Buy to Close',
                    quantity=3,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='0.22', quantity='3')],
                ),
                SimpleNamespace(
                    action='Sell to Close',
                    quantity=3,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='0.10', quantity='3')],
                ),
            ],
        )
        result = _order_result_from_placed_order(order)
        self.assertEqual(result.short_fill_price, 0.22)
        self.assertEqual(result.long_fill_price, 0.10)
        self.assertEqual(result.filled_price, 0.12)
        self.assertEqual(result.filled_quantity, 3)
        self.assertEqual(result.status, 'filled')

    def test_spread_close_debit_requires_negative_neworder_price(self):
        """Documents Tasty SDK sign convention — spread close must use negative price."""
        from decimal import Decimal

        from tastytrade.order import NewOrder, OrderTimeInForce, OrderType
        from tastytrade.utils import PriceEffect

        debit_close = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[],
            price=Decimal('-0.20'),
        )
        credit_wrong = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=OrderType.LIMIT,
            legs=[],
            price=Decimal('0.20'),
        )
        self.assertEqual(debit_close.price_effect, PriceEffect.DEBIT)
        self.assertEqual(credit_wrong.price_effect, PriceEffect.CREDIT)

    def test_futures_single_leg_actions_and_fills(self):
        from brokers.tastytrade_broker import _signed_order_price

        # Verify normalization of single word Buy / Sell
        self.assertEqual(_normalize_leg_action('Buy'), 'BUY')
        self.assertEqual(_normalize_leg_action('Sell'), 'SELL')

        # Verify signed order price
        self.assertEqual(float(_signed_order_price('BUY', 5800.25)), -5800.25)
        self.assertEqual(float(_signed_order_price('SELL', 5800.25)), 5800.25)

        # Single-leg futures Buy order filled
        buy_order = SimpleNamespace(
            id=999001,
            status='Filled',
            size='1',
            price='-5800.25',
            legs=[
                SimpleNamespace(
                    action='Buy',
                    quantity=1,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='5800.25', quantity='1')],
                ),
            ],
        )
        res_buy = _order_result_from_placed_order(buy_order)
        self.assertEqual(res_buy.status, 'filled')
        self.assertEqual(res_buy.filled_price, 5800.25)
        self.assertEqual(res_buy.filled_quantity, 1)

        # Single-leg futures Sell order filled
        sell_order = SimpleNamespace(
            id=999002,
            status='Filled',
            size='2',
            price='5805.50',
            legs=[
                SimpleNamespace(
                    action='Sell',
                    quantity=2,
                    remaining_quantity='0',
                    fills=[SimpleNamespace(fill_price='5805.50', quantity='2')],
                ),
            ],
        )
        res_sell = _order_result_from_placed_order(sell_order)
        self.assertEqual(res_sell.status, 'filled')
        self.assertEqual(res_sell.filled_price, 5805.50)
        self.assertEqual(res_sell.filled_quantity, 2)


if __name__ == '__main__':
    unittest.main()

