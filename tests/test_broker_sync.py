"""Tests for JSON ↔ broker stop reconciliation."""
import unittest
from unittest.mock import MagicMock

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.broker_sync import adopt_active_stop_from_broker


class TestBrokerSync(unittest.TestCase):
    def _open_state(self):
        return state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='test',
            side='C',
            short_symbol='.SPXW260622C7635',
            long_symbol='.SPXW260622C7660',
            short_strike=7635,
            long_strike=7660,
            short_fill=1.45,
            long_fill=0.85,
            net_credit=0.6,
            quantity=5,
            open_order_id='476911300',
        )

    def test_adopt_fills_null_active_stop(self):
        st = self._open_state()
        st['active_stop'] = None
        st['stop_quantity'] = 0

        order = MagicMock()
        order.order_type = 'Stop Limit'
        order.stop_trigger = 2.7
        order.price = -2.8
        order.status = 'Live'
        order.size = 5

        broker = MagicMock()
        broker.find_working_close_order.return_value = OrderResult(
            True, '477400001', 'live', order_quantity=5, raw=order
        )

        self.assertTrue(adopt_active_stop_from_broker(st, broker))
        self.assertEqual(st['active_stop']['order_id'], '477400001')
        self.assertEqual(st['stop_quantity'], 5)
        self.assertTrue(st['active_stop']['adopted_from_broker'])


if __name__ == '__main__':
    unittest.main()
