"""Change 2 — Kill uses spread close, not single-leg limit."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from tests.mock_broker import MockBroker


class TestSpreadKill(unittest.TestCase):
    def test_manual_close_uses_spread_close_order(self):
        broker = MockBroker()
        broker.orders['1001'] = OrderResult(True, '1001', 'working')

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260625P7000',
            long_symbol='.SPXW260625P6975',
            short_strike=7000,
            long_strike=6975,
            short_fill=1.0,
            long_fill=0.2,
            net_credit=0.8,
            quantity=1,
            open_order_id='open-1',
        )
        st['active_stop'] = {
            'order_id': '1001',
            'type': 'STOP_LIMIT',
            'stop_price': 2.0,
            'limit_price': 2.1,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        st['stop_quantity'] = 1

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms1_P_test.json')
            state_mod.save_state(path, st)

            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid = lambda sym: 1.5 if '7000' in sym else 0.3
            prices.get = prices.get_market_mid
            prices.get_spx.return_value = 7100.0

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                mon = StopMonitor(path, broker, prices)
                mon.replace_with_spread_close(reason='manual_close')

            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            leg_limits = [p for p in broker.placed if p[0] == 'limit']
            self.assertEqual(len(spread_closes), 1)
            self.assertEqual(len(leg_limits), 0)
            self.assertEqual(mon.state['status'], 'closed')

    def test_working_spread_close_does_not_chase_long_leg(self):
        broker = MockBroker()
        broker.place_spread_close_order = MagicMock(
            return_value=OrderResult(True, '5001', 'working'),
        )
        broker.orders['5001'] = OrderResult(True, '5001', 'working')

        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260625P7000',
            long_symbol='.SPXW260625P6975',
            short_strike=7000,
            long_strike=6975,
            short_fill=1.0,
            long_fill=0.2,
            net_credit=0.8,
            quantity=1,
            open_order_id='open-1',
        )
        st['status'] = 'closing'
        st['close_mechanism'] = 'manual_close'
        st['spread_close_order_id'] = '5001'

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms1_P_test.json')
            state_mod.save_state(path, st)

            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid = lambda sym: 1.5 if '7000' in sym else 0.3
            prices.get = prices.get_market_mid
            prices.get_spx.return_value = 7100.0

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                mon = StopMonitor(path, broker, prices)
                mon._poll_once()

            leg_limits = [p for p in broker.placed if p[0] == 'limit']
            self.assertEqual(leg_limits, [])
            self.assertEqual(mon.state['spread_close_order_id'], '5001')
            self.assertEqual(mon.state['status'], 'closing')


if __name__ == '__main__':
    unittest.main()
