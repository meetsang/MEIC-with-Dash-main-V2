"""Journal broker records fills locally without broker transmission."""
from __future__ import annotations

import unittest

from brokers.base import ComboLeg
from brokers.journal_broker import JournalBroker
from tests.mock_broker import MockBroker


class TestJournalBroker(unittest.TestCase):
    def test_multi_leg_recorded_at_limit_price(self):
        inner = MockBroker()
        journal = JournalBroker(inner, strategy_name='SKYLINE_SPX')
        legs = [
            ComboLeg('.SPXW260731C6400', 'BUY_TO_OPEN', 1),
            ComboLeg('.SPXW260731C6425', 'SELL_TO_OPEN', 2),
            ComboLeg('.SPXW260731C6450', 'BUY_TO_OPEN', 1),
        ]
        result = journal.place_multi_leg_order(legs, price=-3.15)
        self.assertTrue(result.success)
        self.assertEqual(result.status, 'filled')
        self.assertEqual(result.filled_price, 3.15)
        self.assertFalse(result.transmitted)
        self.assertTrue(result.order_id.startswith('journal-'))
        self.assertEqual(len(inner.placed), 0)

    def test_market_data_delegates_to_inner(self):
        inner = MockBroker()
        inner.prices['SPX'] = 6412.5
        journal = JournalBroker(inner)
        self.assertEqual(journal.get_spx_price(), 6412.5)


if __name__ == '__main__':
    unittest.main()
