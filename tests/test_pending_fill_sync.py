"""Pending open-order fill sync."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.pending_fill_sync import needs_open_order_sync, sync_pending_fills
from brokers.base import OrderResult


class TestPendingFillSync(unittest.TestCase):
    def test_needs_sync_pending_fill(self):
        state = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-81',
            side='P',
            short_symbol='.SPXW260702P7395',
            long_symbol='.SPXW260702P7370',
            short_strike=7395,
            long_strike=7370,
            target_quantity=2,
            open_order_id='480756248',
            limit_credit=0.55,
        )
        self.assertTrue(needs_open_order_sync(state))

    def test_no_sync_when_fully_filled(self):
        state = state_mod.create_pending_state(
            strategy='MANUAL_SPREAD',
            lot='ms-81',
            side='P',
            short_symbol='.SPXW260702P7395',
            long_symbol='.SPXW260702P7370',
            short_strike=7395,
            long_strike=7370,
            target_quantity=2,
            open_order_id='480756248',
            limit_credit=0.55,
        )
        state['status'] = 'open'
        state['filled_quantity'] = 2
        state['short_leg']['fill_price'] = 1.0
        state['long_leg']['fill_price'] = 0.45
        state['open_order']['fully_filled'] = True
        self.assertFalse(needs_open_order_sync(state))

    def test_sync_promotes_to_open(self):
        broker = MagicMock()
        broker.get_order_status.return_value = OrderResult(
            success=True,
            order_id='480756248',
            status='filled',
            filled_quantity=2,
            order_quantity=2,
            filled_price=0.55,
            short_fill_price=1.0,
            long_fill_price=0.45,
        )
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'MANUAL_SPREAD')
            os.makedirs(active)
            state = state_mod.create_pending_state(
                strategy='MANUAL_SPREAD',
                lot='ms-81',
                side='P',
                short_symbol='.SPXW260702P7395',
                long_symbol='.SPXW260702P7370',
                short_strike=7395,
                long_strike=7370,
                target_quantity=2,
                open_order_id='480756248',
                limit_credit=0.55,
            )
            path = os.path.join(active, 'ms-81_P.json')
            state_mod.save_state(path, state)
            with patch.object(state_mod, 'manual_spread_active_dir', return_value=active), patch.object(
                state_mod, 'all_active_dirs', return_value=[active],
            ), patch.object(state_mod, 'iter_active_trade_paths', return_value=[path]), patch(
                'blocks.stop.pending_fill_sync.register_spread_symbols',
            ):
                changed = sync_pending_fills(broker, force=True)
            self.assertEqual(changed, [path])
            updated = state_mod.load_state(path)
            self.assertEqual(updated['status'], 'open')
            self.assertEqual(updated['filled_quantity'], 2)
            self.assertEqual(updated['short_leg']['fill_price'], 1.0)


if __name__ == '__main__':
    unittest.main()
