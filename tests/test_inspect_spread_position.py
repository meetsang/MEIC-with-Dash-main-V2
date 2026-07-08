"""F-9 spread preflight — Tasty position quantity normalization."""
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from brokers.tastytrade_broker import TastyTradeBroker, _signed_position_qty


class TestSignedPositionQty(unittest.TestCase):
    def test_tasty_unsigned_short_leg(self):
        pos = SimpleNamespace(quantity=6, quantity_direction='Short')
        self.assertEqual(_signed_position_qty(pos), -6)

    def test_tasty_unsigned_long_leg(self):
        pos = SimpleNamespace(quantity=6, quantity_direction='Long')
        self.assertEqual(_signed_position_qty(pos), 6)

    def test_already_signed_qty_without_direction(self):
        pos = SimpleNamespace(quantity=-6)
        self.assertEqual(_signed_position_qty(pos), -6)

    def test_zero_qty(self):
        pos = SimpleNamespace(quantity=0, quantity_direction='Short')
        self.assertEqual(_signed_position_qty(pos), 0)


class TestInspectSpreadPosition(unittest.TestCase):
    def _broker_with_positions(self, positions):
        broker = object.__new__(TastyTradeBroker)
        broker.session = MagicMock()
        broker.account = MagicMock()
        broker._run = MagicMock(return_value=positions)
        return broker

    def test_ms185_put_credit_vertical_closable(self):
        """7425 short / 7400 long entry legs — exit is BTC short, STC long."""
        broker = self._broker_with_positions([
            SimpleNamespace(
                symbol='SPXW  260707P07425000',
                quantity=6,
                quantity_direction='Short',
            ),
            SimpleNamespace(
                symbol='SPXW  260707P07400000',
                quantity=6,
                quantity_direction='Long',
            ),
        ])
        state = broker.inspect_spread_position(
            '.SPXW260707P7425',
            '.SPXW260707P7400',
            expected_qty=6,
        )
        self.assertEqual(state, 'closable')

    def test_partial_qty_is_mismatch(self):
        broker = self._broker_with_positions([
            SimpleNamespace(
                symbol='SPXW  260707P07425000',
                quantity=3,
                quantity_direction='Short',
            ),
            SimpleNamespace(
                symbol='SPXW  260707P07400000',
                quantity=6,
                quantity_direction='Long',
            ),
        ])
        state = broker.inspect_spread_position(
            '.SPXW260707P7425',
            '.SPXW260707P7400',
            expected_qty=6,
        )
        self.assertEqual(state, 'mismatch')

    def test_no_positions_is_flat(self):
        broker = self._broker_with_positions([])
        state = broker.inspect_spread_position('S', 'L', expected_qty=1)
        self.assertEqual(state, 'flat')

    def test_unsigned_short_without_direction_was_mismatch(self):
        """Regression: raw +6 on short leg used to fail preflight."""
        broker = self._broker_with_positions([
            SimpleNamespace(symbol='SPXW  260707P07425000', quantity=6),
            SimpleNamespace(
                symbol='SPXW  260707P07400000',
                quantity=6,
                quantity_direction='Long',
            ),
        ])
        state = broker.inspect_spread_position(
            '.SPXW260707P7425',
            '.SPXW260707P7400',
            expected_qty=6,
        )
        self.assertEqual(state, 'mismatch')


if __name__ == '__main__':
    unittest.main()
