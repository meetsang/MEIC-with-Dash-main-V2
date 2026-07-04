"""Unit tests for Phase 1 breach: cancel stop at broker, then place limit."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from mock_broker import MockBroker


class TestBreachLimitClose(unittest.TestCase):
    def _make_monitor(self, broker: MockBroker) -> StopMonitor:
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='test',
            side='C',
            short_symbol='.SPXW260622C07635000',
            long_symbol='.SPXW260622C07660000',
            short_strike=7635,
            long_strike=7660,
            short_fill=1.45,
            long_fill=0.85,
            net_credit=0.6,
            quantity=5,
            open_order_id='476911300',
        )
        st['filled_quantity'] = 5
        st['active_stop'] = {
            'order_id': '1001',
            'type': 'STOP_LIMIT',
            'stop_price': 2.70,
            'limit_price': 2.80,
            'phase': 1,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': 5,
        }
        st['stop_quantity'] = 5
        broker.orders['1001'] = OrderResult(True, '1001', 'working')
        broker._order_seq = 1001

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'trade.json')
        state_mod.save_state(path, st)

        prices = MagicMock(spec=MqttPriceCache)
        prices.get = lambda sym: 3.55 if '7635' in sym else 0.85
        prices.get_market_mid = lambda sym: 3.55 if '7635' in sym else 0.85
        prices.get_spx = lambda: 7600.0
        prices.kill_switch = False

        mon = StopMonitor(path, broker, prices=prices)
        mon.state = st
        mon.json_path = path
        return mon

    def test_replace_with_limit_close_cancel_confirm_then_limit(self):
        broker = MockBroker()
        mon = self._make_monitor(broker)

        mon.replace_with_limit_close(reason='spread_stop_breach')

        self.assertEqual(mon.state['active_stop']['type'], 'LIMIT')
        self.assertEqual(mon.state['active_stop']['order_id'], '1002')
        self.assertEqual(mon.state['active_stop']['limit_price'], 3.60)
        self.assertEqual(broker.orders['1001'].status, 'cancelled')
        actions = [h['action'] for h in mon.state['stop_history']]
        self.assertIn('cancelled', actions)
        self.assertIn('replaced_limit', actions)

    def test_replace_with_limit_close_keeps_json_if_cancel_fails(self):
        broker = MockBroker()
        mon = self._make_monitor(broker)

        def stuck_status(order_id):
            return OrderResult(True, order_id, 'working')

        broker.get_order_status = stuck_status

        mon.replace_with_limit_close(reason='spread_stop_breach')

        self.assertEqual(mon.state['active_stop']['order_id'], '1001')
        self.assertEqual(len([p for p in broker.placed if p[0] == 'limit']), 0)


if __name__ == '__main__':
    unittest.main()
