"""Long-leg close chase: step down instead of re-sending the same limit."""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import LEGACY_LONG_CLOSE_FLOOR, StopMonitor
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
        broker.fetch_option_mids_api = None
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
            self.assertEqual(mon.state['long_close_source'], 'mqtt')

    def test_chase_reprices_legacy_floor_when_quote_available(self):
        st = self._closing_state()
        st['long_close_limit_price'] = LEGACY_LONG_CLOSE_FLOOR
        broker = MagicMock()
        broker.fetch_option_mids_api = None
        broker.get_order_status.return_value = OrderResult(True, '478414695', 'working')
        broker.cancel_order.return_value = OrderResult(True, '478414695', 'cancelled')
        broker.place_limit_order.return_value = OrderResult(True, '478414700', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.35
            prices.get.return_value = 0.35

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.cancel_order.assert_called_once()
            broker.place_limit_order.assert_called_once()
            self.assertEqual(broker.place_limit_order.call_args[0][3], 0.35)
            self.assertEqual(mon.state['long_close_source'], 'mqtt')

    def test_rest_fallback_when_mqtt_missing(self):
        st = self._closing_state()
        st.pop('long_close_order_id', None)
        st.pop('long_close_limit_price', None)
        broker = MagicMock()
        broker.fetch_option_mids_api.return_value = {st['long_leg']['symbol']: 0.40}
        broker.place_limit_order.return_value = OrderResult(True, '478414701', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = None
            prices.get.return_value = None

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.place_limit_order.assert_called_once()
            self.assertEqual(broker.place_limit_order.call_args[0][3], 0.40)
            self.assertEqual(mon.state['long_close_source'], 'broker_rest')

    def test_blocked_when_mqtt_and_rest_missing(self):
        st = self._closing_state()
        st.pop('long_close_order_id', None)
        st.pop('long_close_limit_price', None)
        broker = MagicMock()
        broker.fetch_option_mids_api.return_value = {}
        broker.place_limit_order.return_value = OrderResult(True, '478414701', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = None
            prices.get.return_value = None

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._chase_long_close()

            broker.place_limit_order.assert_not_called()
            self.assertEqual(mon.state['long_close_source'], 'blocked_no_quote')
            self.assertEqual(mon.state['exit_last_step'], 'long_close_blocked_no_quote')

    def test_stale_mqtt_rest_available_no_floor_order(self):
        """Live-sim: stale MQTT cache but REST quote — must not place $0.05."""
        st = self._closing_state()
        st.pop('long_close_order_id', None)
        st.pop('long_close_limit_price', None)
        broker = MagicMock()
        broker.fetch_option_mids_api.return_value = {st['long_leg']['symbol']: 0.38}
        broker.place_limit_order.return_value = OrderResult(True, '478414702', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'closing_C.json')
            state_mod.save_state(path, st)
            cache = MqttPriceCache()
            cache._client = MagicMock()
            cache._connected = True
            now = time.time()
            with cache._lock:
                cache._prices[st['long_leg']['symbol']] = 0.05
                cache._last_msg_at = now - 120.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, cache)
                mon._chase_long_close()

            placed = broker.place_limit_order.call_args[0][3]
            self.assertNotEqual(placed, LEGACY_LONG_CLOSE_FLOOR)
            self.assertGreater(placed, LEGACY_LONG_CLOSE_FLOOR)
            self.assertEqual(mon.state['long_close_source'], 'broker_rest')

    def test_rejected_replaces_one_tick_lower(self):
        st = self._closing_state()
        broker = MagicMock()
        broker.fetch_option_mids_api = None
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
