"""Unit tests for same-side leg overlap guard (legacy check_long_short)."""
import os
import tempfile
import unittest
from unittest import mock

from common.strike_guard import (
    leg_overlap_conflict,
    resolve_leg_overlap,
    shift_spread_strikes,
)
from blocks.stop import state as state_mod


class TestStrikeGuard(unittest.TestCase):
    def _patch_active_dirs(self, *dirs):
        return mock.patch.object(state_mod, 'all_active_dirs', return_value=list(dirs))

    def test_detects_long_equals_existing_short(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-00',
                side='C',
                short_symbol='.SPXW260622C07635000',
                long_symbol='.SPXW260622C07660000',
                short_strike=7635,
                long_strike=7660,
                short_fill=1.45,
                long_fill=0.85,
                net_credit=0.6,
                quantity=5,
                open_order_id='111',
            )
            state_mod.save_state(path, st)

            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW260622C07620000',
                    '.SPXW260622C07635000',
                    'C',
                )

            self.assertIsNotNone(reason)
            self.assertIn('short leg', reason or '')

    def test_detects_short_equals_existing_long(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-00',
                side='P',
                short_symbol='.SPXW260622P07525000',
                long_symbol='.SPXW260622P07500000',
                short_strike=7525,
                long_strike=7500,
                short_fill=1.2,
                long_fill=0.5,
                net_credit=0.7,
                quantity=1,
                open_order_id='222',
            )
            state_mod.save_state(path, st)

            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW260622P07500000',
                    '.SPXW260622P07475000',
                    'P',
                )

            self.assertIsNotNone(reason)
            self.assertIn('long leg', reason or '')

    def test_puts_and_calls_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'call.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-00',
                side='C',
                short_symbol='.SPXW260622C07635000',
                long_symbol='.SPXW260622C07660000',
                short_strike=7635,
                long_strike=7660,
                short_fill=1.45,
                long_fill=0.85,
                net_credit=0.6,
                quantity=5,
                open_order_id='111',
            )
            state_mod.save_state(path, st)

            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW260622P07635000',
                    '.SPXW260622P07610000',
                    'P',
                )

            self.assertIsNone(reason)

    def test_cross_strategy_flip_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            meic_active = os.path.join(tmp, 'meic', 'active')
            manual_active = os.path.join(tmp, 'manual', 'active')
            os.makedirs(meic_active)
            os.makedirs(manual_active)

            meic_path = os.path.join(meic_active, 'meic.json')
            state_mod.save_state(
                meic_path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='11-00',
                    side='P',
                    short_symbol='.SPXW260622P07525000',
                    long_symbol='.SPXW260622P07500000',
                    short_strike=7525,
                    long_strike=7500,
                    short_fill=1.2,
                    long_fill=0.5,
                    net_credit=0.7,
                    quantity=1,
                    open_order_id='333',
                ),
            )

            with self._patch_active_dirs(meic_active, manual_active):
                reason = leg_overlap_conflict(
                    '.SPXW260622P07520000',
                    '.SPXW260622P07525000',
                    'P',
                )

            self.assertIsNotNone(reason)
            self.assertIn('7525', reason or '')

    def test_overlapping_strikes_without_flip_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='11-00',
                    side='P',
                    short_symbol='.SPXW260622P07525000',
                    long_symbol='.SPXW260622P07500000',
                    short_strike=7525,
                    long_strike=7500,
                    short_fill=1.2,
                    long_fill=0.5,
                    net_credit=0.7,
                    quantity=1,
                    open_order_id='444',
                ),
            )

            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW260622P07520000',
                    '.SPXW260622P07495000',
                    'P',
                )

            self.assertIsNone(reason)

    def test_shift_ccs_down_one_step(self):
        ss, ls = shift_spread_strikes('C', 7420, 7445)
        self.assertEqual((ss, ls), (7415, 7440))

    def test_shift_pcs_up_one_step(self):
        ss, ls = shift_spread_strikes('P', 7350, 7325)
        self.assertEqual((ss, ls), (7355, 7330))

    def test_resolve_ccs_long_hits_existing_short(self):
        """12-00 scenario: CCS long 7445 vs 11-00 short 7445 -> shift to 7415/7440."""
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='11-00',
                    side='C',
                    short_symbol='.SPXW260624C7445',
                    long_symbol='.SPXW260624C7470',
                    short_strike=7445,
                    long_strike=7470,
                    short_fill=1.25,
                    long_fill=0.25,
                    net_credit=1.0,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                out = resolve_leg_overlap('260624', 'C', 7420, 7445)
            self.assertIsNotNone(out)
            ss, ls, short_sym, long_sym, shifts = out
            self.assertEqual(shifts, 1)
            self.assertEqual((ss, ls), (7415, 7440))
            self.assertIsNone(leg_overlap_conflict(short_sym, long_sym, 'C'))


if __name__ == '__main__':
    unittest.main()
