"""Partial spread fill: stop after paired units fill, resize when more units fill."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from mock_broker import MockBroker
from tests.fill_sync_fixtures import same_day_trade_env, spxw_option_symbol


class TestPartialFillStop(unittest.TestCase):
    def test_stop_placed_after_partial_spread_then_resized(self):
        broker = MockBroker()
        with same_day_trade_env() as expiry:
            short_sym = spxw_option_symbol(7635, 'C', expiry_yymmdd=expiry)
            long_sym = spxw_option_symbol(7660, 'C', expiry_yymmdd=expiry)
            st = state_mod.create_pending_state(
                strategy='MEIC_IC',
                lot='12-00',
                side='C',
                short_symbol=short_sym,
                long_symbol=long_sym,
                short_strike=7635,
                long_strike=7660,
                target_quantity=10,
                open_order_id='476911300',
                limit_credit=0.60,
            )

            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, 'pending.json')
                state_mod.save_state(path, st)

                prices = MagicMock(spec=MqttPriceCache)
                prices.get = lambda sym: 1.45 if '7635' in sym else 0.85
                prices.get_market_mid = prices.get
                prices.get_spx = lambda: 7600.0
                prices.kill_switch = False

                with patch('common.streamer_symbols.register_spread_symbols'):
                    mon = StopMonitor(path, broker, prices=prices)

                partial = OrderResult(
                    True,
                    '476911300',
                    'partial',
                    filled_quantity=5,
                    order_quantity=10,
                    filled_price=0.60,
                    filled_price_source='broker_leg_math',
                    short_fill_price=1.45,
                    long_fill_price=0.85,
                )
                from blocks.stop.fill_sync import apply_order_result_to_state

                apply_order_result_to_state(mon.state, partial)
                mon._ensure_stop_for_filled_qty()

                self.assertEqual(mon.state['filled_quantity'], 5)
                active_stop = mon.state.get('active_stop') or {}
                self.assertTrue(active_stop.get('order_id'))
                first_oid = active_stop['order_id']
                self.assertEqual(len(broker.placed), 1)

                full = OrderResult(
                    True,
                    '476911300',
                    'filled',
                    filled_quantity=10,
                    order_quantity=10,
                    filled_price=0.60,
                    filled_price_source='broker_leg_math',
                    short_fill_price=1.45,
                    long_fill_price=0.85,
                )
                apply_order_result_to_state(mon.state, full)
                mon._ensure_stop_for_filled_qty()

                self.assertEqual(mon.state['filled_quantity'], 10)
                self.assertGreater(len(broker.placed), 1)
                self.assertNotEqual(mon.state['active_stop']['order_id'], first_oid)


if __name__ == '__main__':
    unittest.main()
