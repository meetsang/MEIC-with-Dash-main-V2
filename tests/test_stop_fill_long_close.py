"""Exchange stop filled → transitions to 'closing', defers long leg close 30s.

Lifecycle: stop fully filled → status='closing' → wait LONG_CLOSE_DELAY_SEC →
place long SELL_TO_CLOSE → chase every ~3s until filled → _finalize_close.
"""
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import LONG_CLOSE_DELAY_SEC, StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache


def _full_stop_fill(order_id: str, qty: int) -> OrderResult:
    return OrderResult(
        True, order_id, 'filled',
        filled_quantity=qty, order_quantity=qty,
    )


class TestStopFillLongClose(unittest.TestCase):
    def _make_state(self, quantity: int = 5):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='jun22-ccs-7635',
            side='C',
            short_symbol='.SPXW260622C07635000',
            long_symbol='.SPXW260622C07660000',
            short_strike=7635,
            long_strike=7660,
            short_fill=1.45,
            long_fill=0.85,
            net_credit=0.6,
            quantity=quantity,
            open_order_id='476911300',
        )
        st['active_stop'] = {
            'order_id': '477426590',
            'type': 'STOP_LIMIT',
            'stop_price': 2.7,
            'limit_price': 2.8,
            'phase': 1,
            'status': 'working',
            'quantity': quantity,
        }
        st['stop_quantity'] = quantity
        return st

    def test_stop_filled_defers_long_close_until_delay_elapsed(self):
        """Full stop fill → closing, no long order until after LONG_CLOSE_DELAY_SEC."""
        st = self._make_state(quantity=5)

        broker = MagicMock()
        broker.get_order_status.return_value = _full_stop_fill('477426590', 5)
        broker.place_limit_order.return_value = OrderResult(True, '477600001', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'open_C.json')
            closed_sub = os.path.join(tmp, 'closed')
            os.makedirs(closed_sub)
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.10
            prices.get.return_value = 0.10
            prices.get_spx.return_value = 7500.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                with patch.object(state_mod, 'closed_dir', return_value=closed_sub):
                    mon = StopMonitor(path, broker, prices)
                    mon._sync_working_stop_order()

            broker.place_limit_order.assert_not_called()
            self.assertEqual(mon.state['status'], 'closing')
            self.assertIsNone(mon.state.get('long_close_order_id'))
            self.assertTrue(os.path.isfile(path))

            mon.state['short_closed_at'] = time.time() - LONG_CLOSE_DELAY_SEC - 1
            mon._chase_long_close()

            broker.place_limit_order.assert_called_once()
            call = broker.place_limit_order.call_args
            self.assertEqual(call[0][0], 'SELL_TO_CLOSE')
            self.assertIn('7660', call[0][1])
            self.assertEqual(call[0][2], 5)
            self.assertEqual(mon.state.get('long_close_order_id'), '477600001')

            closed_path = os.path.join(closed_sub, os.path.basename(path))
            self.assertFalse(os.path.isfile(closed_path))

    def test_partial_stop_fill_does_not_close_long_leg(self):
        """Stop 3/10 filled — stay open; do not start long leg close."""
        st = self._make_state(quantity=10)
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True, '477426590', 'partial',
            filled_quantity=3, order_quantity=10,
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'open_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7500.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._sync_working_stop_order()

            self.assertEqual(mon.state['status'], 'open')
            self.assertIsNone(mon.state.get('short_closed_at'))
            self.assertIsNone(mon.state.get('long_close_order_id'))

    def test_stop_filled_is_idempotent(self):
        """Second stop-fill handler must not place a long close."""
        st = self._make_state(quantity=5)
        broker = MagicMock()
        broker.get_order_status.return_value = _full_stop_fill('477426590', 5)
        broker.place_limit_order.return_value = OrderResult(True, '477600001', 'working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'open_C.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_market_mid.return_value = 0.10
            prices.get.return_value = 0.10
            prices.get_spx.return_value = 7500.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                stop = mon.state['active_stop']
                stop['status'] = 'filled'
                mon.handle_stop_order_update(stop)
                mon.handle_stop_order_update(stop)

            broker.place_limit_order.assert_not_called()
            self.assertEqual(mon.state['status'], 'closing')

    def test_missing_stop_places_new_even_if_broker_has_other_stop(self):
        """Open trade without active_stop must place its own stop, not adopt by symbol."""
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='01-45',
            side='P',
            short_symbol='.SPXW260622P7460',
            long_symbol='.SPXW260622P7435',
            short_strike=7460,
            long_strike=7435,
            short_fill=1.67,
            long_fill=0.37,
            net_credit=1.3,
            quantity=1,
            open_order_id='477742279',
        )
        st['active_stop'] = None
        st['stop_quantity'] = 0

        broker = MagicMock()
        broker.find_working_close_order.return_value = OrderResult(
            True, '477736927', 'live', order_quantity=1,
        )
        broker.place_stop_order.return_value = OrderResult(True, '477799999', 'working')
        broker.get_order_status.return_value = OrderResult(True, '477742279', 'filled')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'open_P.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7475.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                mon = StopMonitor(path, broker, prices)
                mon._ensure_stop_for_filled_qty()

            broker.place_stop_order.assert_called_once()
            broker.find_working_close_order.assert_not_called()
            self.assertEqual(mon.state['active_stop']['order_id'], '477799999')
            self.assertNotIn('adopted_from_broker', mon.state['active_stop'])


if __name__ == '__main__':
    unittest.main()
