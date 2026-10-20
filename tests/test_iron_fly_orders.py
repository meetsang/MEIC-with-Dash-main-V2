"""Unit tests for Iron Fly order construction, tick rounding, and status mapping."""
from __future__ import annotations

import unittest

from strategies.iron_fly.orders import (
    build_iron_fly_close_spec,
    build_iron_fly_open_spec,
    map_broker_status,
    round_credit_down,
    round_debit_up,
)


class TestIronFlyOrders(unittest.TestCase):
    def test_round_credit_down(self):
        self.assertEqual(round_credit_down(15.34, 0.05), 15.30)
        self.assertEqual(round_credit_down(15.35, 0.05), 15.35)
        self.assertEqual(round_credit_down(15.39, 0.05), 15.35)
        self.assertEqual(round_credit_down(15.40, 0.05), 15.40)
        self.assertEqual(round_credit_down(15.34, 0.10), 15.30)
        self.assertEqual(round_credit_down(15.39, 0.10), 15.30)

    def test_round_debit_up(self):
        self.assertEqual(round_debit_up(15.31, 0.05), 15.35)
        self.assertEqual(round_debit_up(15.30, 0.05), 15.30)
        self.assertEqual(round_debit_up(15.36, 0.05), 15.40)
        self.assertEqual(round_debit_up(15.31, 0.10), 15.40)

    def test_build_open_spec(self):
        legs, price, spec = build_iron_fly_open_spec(
            '261002',
            5800.0,
            25,
            20,
            limit_credit=15.38,
            qty=1,
            combo_tick=0.05,
        )
        self.assertEqual(price, 15.35)
        self.assertEqual(len(legs), 4)
        self.assertEqual(legs[0].action, 'SELL_TO_OPEN')
        self.assertEqual(legs[1].action, 'SELL_TO_OPEN')
        self.assertEqual(legs[2].action, 'BUY_TO_OPEN')
        self.assertEqual(legs[3].action, 'BUY_TO_OPEN')

        # Check order spec JSON (§5.1)
        self.assertEqual(spec['orderType'], 'NET_CREDIT')
        self.assertEqual(spec['price'], '15.35')
        self.assertEqual(spec['complexOrderStrategyType'], 'CUSTOM')
        self.assertEqual(len(spec['orderLegCollection']), 4)
        self.assertEqual(spec['orderLegCollection'][0]['instruction'], 'SELL_TO_OPEN')
        self.assertEqual(spec['orderLegCollection'][2]['instruction'], 'BUY_TO_OPEN')

    def test_build_close_spec(self):
        legs, price, spec = build_iron_fly_close_spec(
            '261002',
            5800.0,
            25,
            20,
            limit_debit=12.21,
            qty=1,
            combo_tick=0.05,
        )
        self.assertEqual(price, 12.25)
        self.assertEqual(len(legs), 4)
        self.assertEqual(legs[0].action, 'BUY_TO_CLOSE')
        self.assertEqual(legs[1].action, 'BUY_TO_CLOSE')
        self.assertEqual(legs[2].action, 'SELL_TO_CLOSE')
        self.assertEqual(legs[3].action, 'SELL_TO_CLOSE')

        # Check order spec JSON (§5.1)
        self.assertEqual(spec['orderType'], 'NET_DEBIT')
        self.assertEqual(spec['price'], '12.25')
        self.assertEqual(spec['orderLegCollection'][0]['instruction'], 'BUY_TO_CLOSE')
        self.assertEqual(spec['orderLegCollection'][2]['instruction'], 'SELL_TO_CLOSE')

    def test_map_broker_status(self):
        self.assertEqual(map_broker_status('FILLED'), 'filled')
        self.assertEqual(map_broker_status('WORKING'), 'working')
        self.assertEqual(map_broker_status('QUEUED'), 'working')
        self.assertEqual(map_broker_status('PENDING_ACTIVATION'), 'working')
        self.assertEqual(map_broker_status('CANCELED'), 'cancelled')
        self.assertEqual(map_broker_status('CANCELLED'), 'cancelled')
        self.assertEqual(map_broker_status('REPLACED'), 'replaced')
        self.assertEqual(map_broker_status('REJECTED'), 'rejected')
        # Unknown status must stay 'working' per §5.4
        self.assertEqual(map_broker_status('UNKNOWN_BROKER_STATUS'), 'working')


if __name__ == '__main__':
    unittest.main()
