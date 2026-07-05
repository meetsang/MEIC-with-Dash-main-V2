"""V3 ExchangeStopFilledHandler + LongChaseHandler — fake broker tests."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.handlers.exchange_stop_filled import ExchangeStopFilledHandler
from blocks.stop.v3.handlers.long_chase import LongChaseHandler
from blocks.stop.v3.trade_slot import TradeSlot
from tests.mock_broker import MockBroker


def _open_state(**overrides):
    st = state_mod.create_new_state(
        strategy='MANUAL_SPREAD',
        lot='ms-v3',
        side='C',
        short_symbol='.SPXW260706C7600',
        long_symbol='.SPXW260706C7625',
        short_strike=7600,
        long_strike=7625,
        short_fill=0.82,
        long_fill=0.27,
        net_credit=0.55,
        quantity=3,
        open_order_id='open-v3',
    )
    st['active_stop'] = {
        'order_id': '9001',
        'type': 'STOP_LIMIT',
        'stop_price': 1.7,
        'limit_price': 1.8,
        'phase': 1,
        'status': 'working',
        'quantity': 3,
    }
    st['stop_quantity'] = 3
    st.update(overrides)
    return st


class TestV3ExchangeStopFilled(unittest.TestCase):
    def test_stop_fill_sets_closing_and_exit_fields(self):
        broker = MockBroker()
        broker.orders['9001'] = OrderResult(
            True, '9001', 'filled',
            filled_price=1.75,
            filled_quantity=3,
            order_quantity=3,
        )

        prices = MagicMock()
        prices.get_spx.return_value = 7483.0
        prices.get_market_mid = MagicMock(return_value=0.10)
        prices.get = prices.get_market_mid

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms-v3_C_test.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)
            lane = BrokerLane(max_concurrent=4)

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                ExchangeStopFilledHandler(slot, broker, prices, lane).run()

            self.assertEqual(slot.state.get('status'), 'closing')
            self.assertIsNotNone(slot.state.get('short_closed_at'))
            self.assertEqual(slot.state.get('exit_handler'), 'exchange_stop')
            self.assertEqual(slot.state.get('close_mechanism'), 'exchange_stop')
            self.assertTrue(slot.state.get('exit_started_at'))


class TestV3LongChase(unittest.TestCase):
    def test_long_chase_places_limit_and_closes(self):
        broker = MockBroker()
        broker.prices['.SPXW260706C7625'] = 0.08

        prices = MagicMock()
        prices.get_spx.return_value = 7483.0
        prices.get_market_mid = lambda sym: broker.prices.get(sym, 0.08)
        prices.get = prices.get_market_mid

        st = _open_state(status='closing', close_mechanism='exchange_stop')
        st['short_closed_at'] = time.time() - 60
        st['active_stop']['status'] = 'filled'

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms-v3_C_test.json')
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)
            lane = BrokerLane(max_concurrent=4)

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                LongChaseHandler(slot, broker, prices, lane).run()

            limits = [p for p in broker.placed if p[0] == 'limit']
            self.assertGreaterEqual(len(limits), 1)
            self.assertIsNotNone(slot.state.get('long_close_order_id'))
