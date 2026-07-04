"""Unit tests for stop block phase plugins."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.phases import Phase2NetCreditUpgrade, Phase3SpxProximityClose
from blocks.stop import state as state_mod
from mock_broker import MockBroker


class TestPhases(unittest.TestCase):
    def _make_monitor(self, long_price: float = 1.0) -> StopMonitor:
        broker = MockBroker()
        broker.prices['.SPXW260619P5550'] = 4.0
        broker.prices['.SPXW260619P5520'] = long_price

        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='test',
            side='P',
            short_symbol='.SPXW260619P5550',
            long_symbol='.SPXW260619P5520',
            short_strike=5550,
            long_strike=5520,
            short_fill=4.0,
            long_fill=2.5,
            net_credit=1.5,
            quantity=1,
            open_order_id='1',
        )
        st['active_stop'] = {
            'order_id': '1001',
            'type': 'STOP_LIMIT',
            'stop_price': 8.0,
            'limit_price': 8.1,
            'phase': 1,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'trade.json')
            state_mod.save_state(path, st)
            prices = MagicMock(spec=MqttPriceCache)
            prices.get = lambda sym: broker.prices.get(sym)
            prices.kill_switch = False
            prices.get_spx = lambda: broker.prices.get('SPX')
            mon = StopMonitor(path, broker, prices=prices)
            mon.state = st
            mon.json_path = path
            return mon

    def test_phase2_triggers_on_cheap_long(self):
        mon = self._make_monitor(long_price=0.04)
        phase = Phase2NetCreditUpgrade()
        self.assertTrue(phase.should_activate(mon))

    def test_phase1_skips_rebreach_when_limit_working(self):
        from unittest.mock import MagicMock, patch
        from blocks.stop.phases import Phase1InitialStop

        mon = self._make_monitor()
        mon.state['active_stop'] = {
            'order_id': '2001',
            'type': 'LIMIT',
            'limit_price': 0.20,
            'phase': 1,
            'status': 'working',
        }
        mon.prices.get = MagicMock(return_value=4.45)
        mon.prices.get_market_mid = MagicMock(return_value=0.20)
        mon.kill_switch = False
        mon.replace_with_limit_close = MagicMock()
        mon._sync_active_close_order = MagicMock()

        Phase1InitialStop().execute(mon)

        mon.replace_with_limit_close.assert_not_called()
        mon._sync_active_close_order.assert_called_once()

    def test_phase1_reprices_breach_limit_when_short_mid_moves(self):
        from unittest.mock import MagicMock
        from blocks.stop.phases import Phase1InitialStop

        mon = self._make_monitor()
        mon.state['active_stop'] = {
            'order_id': '2001',
            'type': 'LIMIT',
            'limit_price': 9.0,
            'phase': 1,
            'status': 'working',
        }
        mon.prices.get_market_mid = MagicMock(return_value=9.52)
        mon.replace_with_limit_close = MagicMock()
        mon._sync_active_close_order = MagicMock()

        Phase1InitialStop().execute(mon)

        mon.replace_with_limit_close.assert_called_once_with(reason='breach_limit_reprice')

    def test_phase2_does_not_trigger_when_long_expensive(self):
        mon = self._make_monitor(long_price=0.50)
        phase = Phase2NetCreditUpgrade()
        self.assertFalse(phase.should_activate(mon))


if __name__ == '__main__':
    unittest.main()
