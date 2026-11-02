"""Tests for preflight broker position verification on closing orders.

Ensures that any close order (multi-leg, single-leg, OCO) verifies that
the exact quantity and direction exist in the brokerage account before
submitting, preventing duplicate closes or accidental opens on flat accounts.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import ComboLeg, OrderResult
from brokers.tastytrade_broker import TastyTradeBroker
from tests.mock_broker import MockBroker


class TestClosingOrderPositionPreflight(unittest.TestCase):
    def setUp(self):
        self.legs = [
            ComboLeg(symbol='.SPXW261027P7670', action='BUY_TO_CLOSE', quantity=1),
            ComboLeg(symbol='.SPXW261027P7660', action='SELL_TO_CLOSE', quantity=1),
        ]

    def test_mock_broker_blocks_multi_leg_close_when_flat(self):
        broker = MockBroker()
        broker.spread_position_flat = True

        res = broker.place_multi_leg_order(self.legs, price=-0.55)
        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('position_flat', res.message)
        self.assertFalse(res.transmitted)

    def test_mock_broker_blocks_single_leg_close_when_flat(self):
        broker = MockBroker()
        broker.spread_position_flat = True

        res = broker.place_limit_order('BUY_TO_CLOSE', '.SPXW261027P7670', 1, 0.55)
        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('position_flat', res.message)
        self.assertFalse(res.transmitted)

    def test_tasty_broker_inspect_legs_closable_flat(self):
        broker = object.__new__(TastyTradeBroker)
        broker.account = MagicMock()
        broker.session = MagicMock()
        broker._run = MagicMock(return_value=[])  # Empty positions list

        closable, reason = broker.inspect_legs_closable(self.legs)
        self.assertFalse(closable)
        self.assertEqual(reason, 'position_flat')

    def test_tasty_broker_inspect_legs_closable_success(self):
        broker = object.__new__(TastyTradeBroker)
        broker.account = MagicMock()
        broker.session = MagicMock()

        # Mock positions: short 7670P (-1), long 7660P (+1)
        mock_short_pos = MagicMock()
        mock_short_pos.symbol = 'SPXW  261027P07670000'
        mock_short_pos.quantity = 1
        mock_short_pos.quantity_direction = 'Short'

        mock_long_pos = MagicMock()
        mock_long_pos.symbol = 'SPXW  261027P07660000'
        mock_long_pos.quantity = 1
        mock_long_pos.quantity_direction = 'Long'

        broker._run = MagicMock(return_value=[mock_short_pos, mock_long_pos])

        closable, reason = broker.inspect_legs_closable(self.legs)
        self.assertTrue(closable)
        self.assertEqual(reason, 'closable')

    def test_tasty_broker_inspect_legs_closable_insufficient_qty(self):
        broker = object.__new__(TastyTradeBroker)
        broker.account = MagicMock()
        broker.session = MagicMock()

        # Needs 2 contracts, but account only holds 1
        legs_need_2 = [
            ComboLeg(symbol='.SPXW261027P7670', action='BUY_TO_CLOSE', quantity=2),
            ComboLeg(symbol='.SPXW261027P7660', action='SELL_TO_CLOSE', quantity=2),
        ]

        mock_short_pos = MagicMock()
        mock_short_pos.symbol = 'SPXW  261027P07670000'
        mock_short_pos.quantity = 1
        mock_short_pos.quantity_direction = 'Short'

        mock_long_pos = MagicMock()
        mock_long_pos.symbol = 'SPXW  261027P07660000'
        mock_long_pos.quantity = 1
        mock_long_pos.quantity_direction = 'Long'

        broker._run = MagicMock(return_value=[mock_short_pos, mock_long_pos])

        closable, reason = broker.inspect_legs_closable(legs_need_2)
        self.assertFalse(closable)
        self.assertIn('insufficient_short_qty', reason)

    def test_tasty_broker_inspect_legs_closable_mismatch_direction(self):
        broker = object.__new__(TastyTradeBroker)
        broker.account = MagicMock()
        broker.session = MagicMock()

        # Order wants to BUY_TO_CLOSE 7670P, but account holds LONG (+1) instead of SHORT
        mock_pos = MagicMock()
        mock_pos.symbol = 'SPXW  261027P07670000'
        mock_pos.quantity = 1
        mock_pos.quantity_direction = 'Long'

        broker._run = MagicMock(return_value=[mock_pos])

        closable, reason = broker.inspect_legs_closable(self.legs)
        self.assertFalse(closable)
        self.assertIn('position_mismatch_long', reason)

    def test_tasty_broker_place_multi_leg_blocks_transmission_when_flat(self):
        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock()
        with patch.object(broker, 'inspect_legs_closable', return_value=(False, 'position_flat')):
            res = broker.place_multi_leg_order(self.legs, price=-0.55)

        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('position_flat', res.message)
        self.assertFalse(res.transmitted)
        # Verify no order placement call was made to tastytrade API
        broker._run.assert_not_called()

    def test_tasty_broker_place_single_leg_blocks_transmission_when_flat(self):
        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock()
        with patch.object(broker, 'get_single_leg_position_qty', return_value=0):
            res = broker._place_single_leg_order('BUY_TO_CLOSE', '.SPXW261027P7670', 1)

        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('position_flat', res.message)
        self.assertFalse(res.transmitted)
        broker._run.assert_not_called()

    def test_tasty_broker_place_oco_blocks_transmission_when_flat(self):
        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock()
        with patch.object(broker, 'get_single_leg_position_qty', return_value=0):
            res = broker.place_oco_order('.SPXW261027P7670', 1, profit_price=0.20, stop_price=1.80, limit_price=2.00)

        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('position_flat', res.message)
        self.assertFalse(res.transmitted)
        broker._run.assert_not_called()

    def test_drma_hm_chase_handles_preflight_flat_gracefully(self):
        from strategies.drma_hm.strategy import DRMAHMStrategy
        from strategies.drma_hm.config import DRMAHMConfig

        strat = DRMAHMStrategy(DRMAHMConfig(name='DRMA_HM_SPX'))
        mock_broker = MagicMock()
        mock_broker.place_multi_leg_order.return_value = OrderResult(
            False, None, 'rejected_preflight', message='spread_not_closable_position_flat', transmitted=False
        )

        trade = {
            'trade_id': 'credit_bull_test',
            'status': 'open',
            'quantity': 1,
        }

        filled, oid, px = strat._chase_options_spread_close(
            close_broker=mock_broker,
            trade=trade,
            close_legs=self.legs,
            is_debit=False,
            initial_close_price=-0.55,
            qty=1,
            max_attempts=1,
            wait_sec=0.0,
        )

        # Should recognize position is already flat and return filled=True without error
        self.assertTrue(filled)
        self.assertEqual(oid, 'already_flat_on_broker')
        self.assertEqual(px, 0.55)

    def test_manual_credit_spread_close_preflight_blocks_when_flat_and_finalizes_trade(self):
        import tempfile
        from datetime import date
        from common.manual_close import close_structure

        tmp = tempfile.mkdtemp()
        active = os.path.join(tmp, 'trades', 'active', 'MANUAL_SPX')
        os.makedirs(active, exist_ok=True)
        today = date.today().isoformat()
        trade = {
            'trade_id': 'manual_pcs_1',
            'strategy': 'MANUAL_SPX',
            'date': today,
            'status': 'open',
            'paper': False,
            'structures': [{
                'structure_id': 'PCS1',
                'name': 'PCS1',
                'status': 'filled',
                'legs': [
                    {'symbol': '.SPXW261027P7670', 'action': 'SELL_TO_OPEN', 'qty': 1},
                    {'symbol': '.SPXW261027P7660', 'action': 'BUY_TO_OPEN', 'qty': 1},
                ],
            }],
        }
        with open(os.path.join(active, f'manual_{today}.json'), 'w', encoding='utf-8') as f:
            json.dump(trade, f)

        mock_broker = MagicMock()
        mock_broker.place_multi_leg_order.return_value = OrderResult(
            False, None, 'rejected_preflight', message='spread_not_closable_position_flat', transmitted=False
        )

        with patch('common.manual_close.get_shared_broker_for_strategy', return_value=mock_broker):
            res = close_structure('MANUAL_SPX', 'PCS1', root=tmp, trade_id='manual_pcs_1')

        self.assertTrue(res.success)
        self.assertEqual(res.order_id, 'already_flat_on_broker')

        # Trade file on disk should be finalized as closed and moved to history
        hist = os.path.join(tmp, 'trades', 'history', 'MANUAL_SPX')
        with open(os.path.join(hist, f'manual_{today}.json'), encoding='utf-8') as f:
            saved = json.load(f)
        self.assertEqual(saved['status'], 'closed')
        self.assertEqual(saved['structures'][0]['status'], 'closed')

    def test_meic_spread_close_preflight_blocks_when_flat(self):
        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock()
        broker.account = MagicMock()
        broker.session = MagicMock()

        # Both short and long positions are 0 on brokerage
        with patch.object(broker, 'inspect_legs_closable', return_value=(False, 'position_flat')):
            res = broker.place_spread_close_order('.SPXW261027P7670', '.SPXW261027P7660', 1, 0.55)

        self.assertFalse(res.success)
        self.assertEqual(res.status, 'rejected_preflight')
        self.assertIn('flat', res.message)
        self.assertFalse(res.transmitted)
        broker._run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
