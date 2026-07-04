"""Fill sync throttling for pending manual / tranche opens."""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.fill_sync import (
    FILL_SYNC_INTERVAL_SEC,
    PENDING_FILL_SYNC_INTERVAL_SEC,
    apply_order_result_to_state,
    fill_sync_interval_sec,
    stop_order_fully_filled,
    sync_open_order,
)


class TestFillSync(unittest.TestCase):
    def _pending_state(self):
        st = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260624P7305',
            long_symbol='.SPXW260624P7280',
            short_strike=7305,
            long_strike=7280,
            target_quantity=1,
            open_order_id='478445940',
            limit_credit=0.5,
        )
        st['open_order']['last_sync_epoch'] = time.time()
        return st

    def test_pending_fill_uses_fast_interval(self):
        st = self._pending_state()
        self.assertEqual(fill_sync_interval_sec(st), PENDING_FILL_SYNC_INTERVAL_SEC)

    def test_open_filled_uses_fast_interval_when_leg_prices_missing(self):
        """Recovery path when fully_filled but leg prices missing — still 3s, not 30s."""
        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='ms-1',
            side='P',
            short_symbol='.SPXW260624P7305',
            long_symbol='.SPXW260624P7280',
            short_strike=7305,
            long_strike=7280,
            short_fill=0.77,
            long_fill=0.27,
            net_credit=0.5,
            quantity=1,
            open_order_id='478445940',
        )
        st['open_order'] = {'fully_filled': True, 'status': 'filled'}
        st['long_leg']['fill_price'] = 0
        self.assertEqual(fill_sync_interval_sec(st), FILL_SYNC_INTERVAL_SEC)
        self.assertEqual(FILL_SYNC_INTERVAL_SEC, 3)

    def test_partial_fill_not_fully_filled(self):
        """9 of 10 spread units — not a full fill; entry sync must continue."""
        st = self._pending_state()
        st['quantity'] = 10
        result = OrderResult(
            True,
            '478445940',
            'partial',
            filled_quantity=9,
            order_quantity=10,
            filled_price=0.5,
            short_fill_price=0.77,
            long_fill_price=0.27,
        )
        apply_order_result_to_state(st, result)
        self.assertEqual(st['filled_quantity'], 9)
        self.assertFalse(st['open_order']['fully_filled'])
        self.assertEqual(st['status'], 'open')
        self.assertEqual(fill_sync_interval_sec(st), PENDING_FILL_SYNC_INTERVAL_SEC)

    def test_full_fill_requires_all_units(self):
        st = self._pending_state()
        st['quantity'] = 10
        result = OrderResult(
            True,
            '478445940',
            'filled',
            filled_quantity=10,
            order_quantity=10,
            filled_price=0.5,
            short_fill_price=0.77,
            long_fill_price=0.27,
        )
        apply_order_result_to_state(st, result)
        self.assertTrue(st['open_order']['fully_filled'])

    def test_stop_partial_not_fully_filled(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='12-00',
            side='C',
            short_symbol='.SPXW260622C07635000',
            long_symbol='.SPXW260622C07660000',
            short_strike=7635,
            long_strike=7660,
            short_fill=1.45,
            long_fill=0.85,
            net_credit=0.6,
            quantity=10,
            open_order_id='476911300',
        )
        st['active_stop'] = {'order_id': '477426590', 'quantity': 10}
        partial = OrderResult(
            True, '477426590', 'partial',
            filled_quantity=4, order_quantity=10,
        )
        self.assertFalse(stop_order_fully_filled(st, partial))

    def test_stop_full_fill_all_units(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='12-00',
            side='C',
            short_symbol='.SPXW260622C07635000',
            long_symbol='.SPXW260622C07660000',
            short_strike=7635,
            long_strike=7660,
            short_fill=1.45,
            long_fill=0.85,
            net_credit=0.6,
            quantity=10,
            open_order_id='476911300',
        )
        st['active_stop'] = {'order_id': '477426590', 'quantity': 10}
        full = OrderResult(
            True, '477426590', 'filled',
            filled_quantity=10, order_quantity=10,
        )
        self.assertTrue(stop_order_fully_filled(st, full))

    def test_epoch_zero_always_syncs(self):
        st = self._pending_state()
        st['open_order']['last_sync_epoch'] = 0
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            True, '478445940', 'filled', filled_quantity=1, order_quantity=1,
            short_fill_price=0.77, long_fill_price=0.27, filled_price=0.5,
        )
        changed, _ = sync_open_order(st, broker, force=False)
        self.assertTrue(changed)
        broker.get_order_status.assert_called_once()

    def test_apply_filled_spread_opens_trade(self):
        st = self._pending_state()
        result = OrderResult(
            True,
            '478445940',
            'filled',
            filled_quantity=1,
            order_quantity=1,
            filled_price=0.5,
            short_fill_price=0.77,
            long_fill_price=0.27,
        )
        self.assertTrue(apply_order_result_to_state(st, result))
        self.assertEqual(st['status'], 'open')
        self.assertEqual(st['filled_quantity'], 1)
        self.assertEqual(st['short_leg']['fill_price'], 0.77)


if __name__ == '__main__':
    unittest.main()
