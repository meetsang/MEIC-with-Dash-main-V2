"""Long-leg close chase: step down instead of re-sending the same limit."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache


class TestLongCloseChase(unittest.TestCase):
    def _closing_state(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='11-00',
            side='C',
            short_symbol='.SPXW260624C7445',
            long_symbol='.SPXW260624C7470',
            short_strike=7445,
            long_strike=7470,
            short_fill=1.5,
            long_fill=0.25,
            net_credit=1.25,
            quantity=1,
            open_order_id='478365441',
        )
        st['status'] = 'closing'
        st['short_closed_at'] = 1.0
        st['long_close_order_id'] = '478414695'
        st['long_close_limit_price'] = 0.15
        st['long_close_attempts'] = 1
        return st

    def test_chase_steps_down_when_mid_matches_working_limit(self):
        st = self._closing_state()
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, '478414695', 'working')
        broker.cancel_order.return_value = OrderResult(True, '478414695', 'cancelled')
        broker.place_limit_order.return_value = OrderResult(True, '478414700', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.15
            prices.get.return_value = 0.15

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.cancel_order.assert_called_once()
            broker.place_limit_order.assert_called_once()
            self.assertEqual(broker.place_limit_order.call_args[0][3], 0.10)
            self.assertEqual(mon.state['long_close_limit_price'], 0.10)

    def test_chase_skips_when_already_at_floor_and_mid_unchanged(self):
        st = self._closing_state()
        st['long_close_limit_price'] = 0.05
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, '478414695', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.15
            prices.get.return_value = 0.15

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.cancel_order.assert_not_called()
            broker.place_limit_order.assert_not_called()

    def test_rejected_replaces_one_tick_lower(self):
        st = self._closing_state()
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(True, '478414695', 'rejected')
        broker.place_limit_order.return_value = OrderResult(True, '478414701', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.15
            prices.get.return_value = 0.15

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.place_limit_order.assert_called_once()
            self.assertEqual(broker.place_limit_order.call_args[0][3], 0.10)


if __name__ == '__main__':
    unittest.main()
