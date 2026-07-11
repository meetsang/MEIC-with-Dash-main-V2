"""One active opening order per slot guard tests."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from blocks.entry.opening_duplicate_guard import (
    ENTRY_DUPLICATE_RISK_BLOCKED,
    assert_replacement_allowed,
    record_entry_attempt,
    update_latest_attempt_status,
)
from blocks.stop import state as state_mod


class TestOpeningDuplicateGuard(unittest.TestCase):
    def _empty_state(self):
        return state_mod.create_pending_state(
            strategy='MEIC_IC',
            lot='11-00',
            side='P',
            short_symbol='.SPXW260710P7525',
            long_symbol='.SPXW260710P7500',
            short_strike=7525,
            long_strike=7500,
            target_quantity=1,
            open_order_id='1',
            limit_credit=1.0,
        )

    def test_initial_place_allowed(self):
        broker = MagicMock()
        allowed, reason = assert_replacement_allowed(
            broker, {}, short_symbol='a', long_symbol='b', quantity=1, is_initial_place=True,
        )
        self.assertTrue(allowed)
        self.assertEqual(reason, '')

    def test_previous_working_order_blocks(self):
        state = self._empty_state()
        record_entry_attempt(state, attempt=1, order_id='111', status='working')
        broker = MagicMock()
        broker.find_working_open_spread_orders.return_value = []
        broker.inspect_spread_position.return_value = 'flat'
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'previous_order_working')

    def test_previous_unknown_order_blocks(self):
        state = self._empty_state()
        record_entry_attempt(state, attempt=1, order_id='111', status='visibility_unknown')
        broker = MagicMock()
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'previous_visibility_unknown')

    def test_partial_fill_blocks(self):
        state = self._empty_state()
        record_entry_attempt(state, attempt=1, order_id='111', status='partial', filled_quantity=1)
        broker = MagicMock()
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'previous_partial_or_filled')

    def test_cancel_unconfirmed_blocks(self):
        state = self._empty_state()
        record_entry_attempt(state, attempt=1, order_id='111', status='cancelled', terminal_confirmed=False)
        broker = MagicMock()
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'previous_cancel_unconfirmed')

    def test_terminal_cancel_confirmed_allows(self):
        state = self._empty_state()
        record_entry_attempt(
            state, attempt=1, order_id='111', status='cancelled', terminal_confirmed=True, filled_quantity=0,
        )
        broker = MagicMock()
        broker.find_working_open_spread_orders.return_value = []
        broker.inspect_spread_position.return_value = 'flat'
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertTrue(allowed)

    def test_broker_position_blocks(self):
        state = self._empty_state()
        update_latest_attempt_status(state, status='cancelled', terminal_confirmed=True, filled_quantity=0)
        broker = MagicMock()
        broker.inspect_spread_position.return_value = 'closable'
        allowed, reason = assert_replacement_allowed(
            broker, state,
            short_symbol=state['short_leg']['symbol'],
            long_symbol=state['long_leg']['symbol'],
            quantity=1,
        )
        self.assertFalse(allowed)
        self.assertEqual(reason, 'broker_position_already_open')

    def test_entry_attempt_chain_persisted(self):
        state = self._empty_state()
        record_entry_attempt(state, attempt=1, order_id='111')
        self.assertEqual(len(state['entry_attempts']), 1)
        self.assertEqual(state['current_open_order_id'], '111')
        self.assertEqual(ENTRY_DUPLICATE_RISK_BLOCKED, 'ENTRY_DUPLICATE_RISK_BLOCKED')


if __name__ == '__main__':
    unittest.main()
