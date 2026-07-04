"""Regression: stop monitor no longer syncs entry fills on load."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache


class TestStopMonitorPending(unittest.TestCase):
    def test_on_load_does_not_sync_open_order(self):
        state = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='integration-session',
            side='P',
            short_symbol='.SPXW260622P7410',
            long_symbol='.SPXW260622P7385',
            short_strike=7410,
            long_strike=7385,
            target_quantity=1,
            open_order_id='477425526',
            limit_credit=1.85,
        )
        broker = MagicMock()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'pending_P.json')
            state_mod.save_state(path, state)
            prices = MagicMock(spec=MqttPriceCache)

            with patch('common.streamer_symbols.register_spread_symbols'):
                monitor = StopMonitor(path, broker, prices)
                monitor._on_load()

            self.assertEqual(monitor.state['status'], 'pending_fill')
            broker.get_order_status.assert_not_called()

    def test_on_load_does_not_resize_from_open_order(self):
        """Entry monitor hands off full fills — stop monitor must not grow qty from open sync."""
        state = state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='jun22-ccs-partial',
            side='C',
            short_symbol='.SPXW260622C07635000',
            long_symbol='.SPXW260622C07660000',
            short_strike=7635,
            long_strike=7660,
            target_quantity=5,
            open_order_id='476911300',
            limit_credit=0.6,
        )
        state['status'] = 'open'
        state['filled_quantity'] = 2
        state['stop_quantity'] = 2
        state['short_leg']['fill_price'] = 1.45
        state['short_leg']['two_x_short'] = 2.9
        state['long_leg']['fill_price'] = 0.85
        state['active_stop'] = {
            'order_id': '477426590',
            'type': 'STOP_LIMIT',
            'stop_price': 2.7,
            'limit_price': 2.8,
            'phase': 1,
            'status': 'working',
            'quantity': 2,
        }

        broker = MagicMock()
        broker.get_order_status.return_value = MagicMock(success=True, status='working')

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'partial_C.json')
            state_mod.save_state(path, state)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get_spx.return_value = 7500.0

            with patch('common.streamer_symbols.register_spread_symbols'):
                monitor = StopMonitor(path, broker, prices)
                monitor._on_load()

            self.assertEqual(monitor.state['filled_quantity'], 2)
            broker.cancel_order.assert_not_called()
            broker.place_stop_order.assert_not_called()


if __name__ == '__main__':
    unittest.main()
