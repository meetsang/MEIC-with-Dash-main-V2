"""Tests for explicit repair-only broker stop adoption."""
import unittest
from unittest.mock import MagicMock

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.broker_sync import expected_exchange_stop_prices, repair_orphan_stop


def _broker_order_raw(stop_trigger: float, limit_price: float, qty: int = 5):
    raw = MagicMock()
    raw.order_type = 'Stop Limit'
    raw.stop_trigger = stop_trigger
    raw.price = -limit_price
    raw.status = 'Live'
    raw.size = qty
    return raw


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

    def test_repair_apply_adopts_matching_orphan(self):
        st = self._open_state()
        st['active_stop'] = None
        st['stop_quantity'] = 0
        exp_stop, exp_limit = expected_exchange_stop_prices(st)

        broker = MagicMock()
        broker.find_working_close_orders.return_value = [
            OrderResult(
                True,
                '477400001',
                'live',
                order_quantity=5,
                raw=_broker_order_raw(exp_stop, exp_limit, qty=5),
            )
        ]

        outcome = repair_orphan_stop(st, broker, apply=True)
        self.assertEqual(outcome.status, 'adopted')
        self.assertEqual(st['active_stop']['order_id'], '477400001')
        self.assertEqual(st['stop_quantity'], 5)
        self.assertTrue(st['active_stop']['adopted_from_broker'])
        self.assertTrue(st['active_stop']['repair_mode'])


if __name__ == '__main__':
    unittest.main()
