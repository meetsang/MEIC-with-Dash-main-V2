"""V3 ManualKillHandler — fake broker tests (design §11.1 Step 3/7)."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.command_claim import detect_and_claim_close_command
from blocks.stop.v3.handlers.manual_kill import ManualKillHandler
from blocks.stop.v3.quotes import resolve_spread_close_debit
from blocks.stop.v3.trade_slot import TradeSlot, merge_disk_state
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


class TestV3QuoteFallback(unittest.TestCase):
    def test_mqtt_primary(self):
        prices = MagicMock()
        prices.get_market_mid = lambda sym: 0.22 if '7600' in sym else 0.10
        prices.get = prices.get_market_mid
        quote = resolve_spread_close_debit(_open_state(), prices, MockBroker())
        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, 'mqtt')

    def test_broker_rest_when_mqtt_missing(self):
        prices = MagicMock()
        prices.get_market_mid = MagicMock(return_value=None)
        prices.get = MagicMock(return_value=None)
        broker = MockBroker()
        broker.fetch_option_mids_api = MagicMock(return_value={
            '.SPXW260706C7600': 0.30,
            '.SPXW260706C7625': 0.10,
        })
        quote = resolve_spread_close_debit(_open_state(), prices, broker)
        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, 'broker_rest')

    def test_emergency_when_all_missing(self):
        prices = MagicMock()
        prices.get_market_mid = MagicMock(return_value=None)
        prices.get = MagicMock(return_value=None)
        broker = MockBroker()
        quote = resolve_spread_close_debit(_open_state(), prices, broker)
        self.assertIsNotNone(quote)
        self.assertEqual(quote.source, 'emergency_offset')


class TestV3ManualKillHandler(unittest.TestCase):
    def test_manual_kill_spread_close_with_broker_rest(self):
        broker = MockBroker()
        broker.orders['9001'] = OrderResult(True, '9001', 'working')
        broker.fetch_option_mids_api = MagicMock(return_value={
            '.SPXW260706C7600': 0.22,
            '.SPXW260706C7625': 0.10,
        })

        prices = MagicMock()
        prices.get_market_mid = MagicMock(return_value=None)
        prices.get = MagicMock(return_value=None)
        prices.get_spx.return_value = 7483.0

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'ms-v3_C_test.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)
            lane = BrokerLane(max_concurrent=4)

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                handler = ManualKillHandler(slot, broker, prices, lane)
                handler.run(reason='manual_close')

            self.assertEqual(slot.state.get('status'), 'closed')
            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertEqual(len(spread_closes), 1)
            self.assertTrue(slot.state.get('close_only_mode'))


class TestV3CommandClaim(unittest.TestCase):
    def test_claim_close_command_sets_close_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_dir = os.path.join(tmp, 'commands')
            os.makedirs(cmd_dir, exist_ok=True)
            path = os.path.join(tmp, 't_C_test.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)

            cmd_path = os.path.join(cmd_dir, 't_C_test.json.close.json')
            with open(cmd_path, 'w', encoding='utf-8') as f:
                f.write('{"close_mechanism":"manual_close"}')

            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp):
                claimed, mech = detect_and_claim_close_command(slot)

            self.assertTrue(claimed)
            self.assertEqual(mech, 'manual_close')
            self.assertTrue(slot.state.get('close_only_mode'))
            self.assertFalse(os.path.exists(cmd_path))


class TestV3TradeSlotMerge(unittest.TestCase):
    def test_merge_skips_when_mtime_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)
            slot.state['lot'] = 'mutated'
            merge_disk_state(slot)
            self.assertEqual(slot.state['lot'], 'mutated')
