"""Unit tests for same-side leg overlap guard (legacy check_long_short)."""
import datetime
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
    def setUp(self):
        p = mock.patch('common.session_cleanup._central_today', return_value=datetime.date(2026, 1, 1))
        p.start()
        self.addCleanup(p.stop)

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

    def test_identical_spread_allowed_by_default(self):
        """User requirement: identical vertical spread allowed by default on the same side."""
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-45',
                    side='P',
                    short_symbol='.SPXW261002P7700',
                    long_symbol='.SPXW261002P7675',
                    short_strike=7700,
                    long_strike=7675,
                    short_fill=1.67,
                    long_fill=0.47,
                    net_credit=1.2,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW261002P7700',
                    '.SPXW261002P7675',
                    'P',
                )
            self.assertIsNone(reason)

    def test_identical_spread_blocked_when_explicitly_disallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-45',
                    side='P',
                    short_symbol='.SPXW261002P7700',
                    long_symbol='.SPXW261002P7675',
                    short_strike=7700,
                    long_strike=7675,
                    short_fill=1.67,
                    long_fill=0.47,
                    net_credit=1.2,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                reason = leg_overlap_conflict(
                    '.SPXW261002P7700',
                    '.SPXW261002P7675',
                    'P',
                    allow_same_spread=False,
                )
            self.assertIsNotNone(reason)
            self.assertIn('identical spread', reason or '')

    def test_leg_reversal_blocked_user_example(self):
        """
        User requirement:
        PCS 7700/7675 is open.
        PCS 7725/7700 must NOT be allowed (db 7700 reverses cr 7700).
        PCS 7675/7650 must NOT be allowed (cr 7675 reverses db 7675).
        """
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-45',
                    side='P',
                    short_symbol='.SPXW261002P7700',
                    long_symbol='.SPXW261002P7675',
                    short_strike=7700,
                    long_strike=7675,
                    short_fill=1.67,
                    long_fill=0.47,
                    net_credit=1.2,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                # 1. Exact same strikes: ALLOWED
                self.assertIsNone(
                    leg_overlap_conflict('.SPXW261002P7700', '.SPXW261002P7675', 'P')
                )
                # 2. Debit 7700 vs Credit 7700: BLOCKED
                reason_rev1 = leg_overlap_conflict('.SPXW261002P7725', '.SPXW261002P7700', 'P')
                self.assertIsNotNone(reason_rev1)
                self.assertIn('short leg', reason_rev1 or '')

                # 3. Credit 7675 vs Debit 7675: BLOCKED
                reason_rev2 = leg_overlap_conflict('.SPXW261002P7675', '.SPXW261002P7650', 'P')
                self.assertIsNotNone(reason_rev2)
                self.assertIn('long leg', reason_rev2 or '')

    def test_closed_trade_does_not_block_any_strike(self):
        """
        User requirement 1: Even if a trade is closed that strike is never picked up.
        With fix: closed trades (status closed, close dict, or 0 quantity) NEVER block.
        """
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'closed_trade.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='10-45',
                side='P',
                short_symbol='.SPXW261002P7700',
                long_symbol='.SPXW261002P7675',
                short_strike=7700,
                long_strike=7675,
                short_fill=1.67,
                long_fill=0.47,
                net_credit=1.2,
                quantity=1,
                open_order_id='111',
            )
            st['status'] = 'closed'
            st['close'] = {'reason': 'profit_target_oco', 'timestamp': '2026-10-02T10:00:00-05:00'}
            state_mod.save_state(path, st)

            with self._patch_active_dirs(active):
                # 1. Same strikes allowed
                self.assertIsNone(
                    leg_overlap_conflict('.SPXW261002P7700', '.SPXW261002P7675', 'P')
                )
                # 2. Reversing strikes also allowed because former trade is CLOSED
                self.assertIsNone(
                    leg_overlap_conflict('.SPXW261002P7725', '.SPXW261002P7700', 'P')
                )
                self.assertIsNone(
                    leg_overlap_conflict('.SPXW261002P7675', '.SPXW261002P7650', 'P')
                )

    def test_zero_holding_quantity_trade_does_not_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'scaled_trade.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='10-45',
                side='P',
                short_symbol='.SPXW261002P7700',
                long_symbol='.SPXW261002P7675',
                short_strike=7700,
                long_strike=7675,
                short_fill=1.67,
                long_fill=0.47,
                net_credit=1.2,
                quantity=1,
                open_order_id='111',
            )
            st['holding_quantity'] = 0
            state_mod.save_state(path, st)

            with self._patch_active_dirs(active):
                self.assertIsNone(
                    leg_overlap_conflict('.SPXW261002P7725', '.SPXW261002P7700', 'P')
                )

    def test_resolve_leg_overlap_zero_shifts_for_identical_spread(self):
        """Identical spread returns immediately with 0 shifts."""
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-45',
                    side='P',
                    short_symbol='.SPXW261002P7700',
                    long_symbol='.SPXW261002P7675',
                    short_strike=7700,
                    long_strike=7675,
                    short_fill=1.67,
                    long_fill=0.47,
                    net_credit=1.2,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                out = resolve_leg_overlap('261002', 'P', 7700, 7675)
            self.assertIsNotNone(out)
            ss, ls, short_sym, long_sym, shifts = out
            self.assertEqual(shifts, 0)
            self.assertEqual((ss, ls), (7700, 7675))

    def test_resolve_leg_overlap_reverse_direction(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            path = os.path.join(active, 'existing.json')
            state_mod.save_state(
                path,
                state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-45',
                    side='C',
                    short_symbol='.SPXW260820C7705',
                    long_symbol='.SPXW260820C7730',
                    short_strike=7705,
                    long_strike=7730,
                    short_fill=1.25,
                    long_fill=0.25,
                    net_credit=1.0,
                    quantity=1,
                    open_order_id='111',
                ),
            )
            with self._patch_active_dirs(active):
                # Reverse direction for Calls shifts UP (7680/7705 -> 7685/7710)
                out = resolve_leg_overlap('260820', 'C', 7680, 7705, direction='reverse')
            self.assertIsNotNone(out)
            ss, ls, short_sym, long_sym, shifts = out
            self.assertEqual(shifts, 1)
            self.assertEqual((ss, ls), (7685, 7710))
            self.assertIsNone(leg_overlap_conflict(short_sym, long_sym, 'C'))

    def test_paper_and_live_isolation_leg_overlap(self):
        """
        Paper active trades should only block paper trades.
        Live active trades should only block live trades.
        If one is paper and one is live, neither blocks the other.
        """
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            meic_path = os.path.join(active, 'meic_paper.json')
            st_paper = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-00',
                side='C',
                short_symbol='.SPXW261019C7755',
                long_symbol='.SPXW261019C7780',
                short_strike=7755,
                long_strike=7780,
                short_fill=1.45,
                long_fill=0.85,
                net_credit=0.6,
                quantity=1,
                open_order_id='journal-12345',
            )
            st_paper['paper'] = True
            state_mod.save_state(meic_path, st_paper)

            with self._patch_active_dirs(active):
                # 1. Live candidate (paper=False) vs Paper active trade: NOT BLOCKED
                reason_live = leg_overlap_conflict(
                    '.SPXW261019C7730',
                    '.SPXW261019C7755',
                    'C',
                    paper=False,
                )
                self.assertIsNone(reason_live)
                out_live = resolve_leg_overlap('261019', 'C', 7730, 7755, paper=False)
                self.assertIsNotNone(out_live)
                ss, ls, _, _, shifts = out_live
                self.assertEqual(shifts, 0)
                self.assertEqual((ss, ls), (7730, 7755))

                # 2. Paper candidate (paper=True) vs Paper active trade: BLOCKED & SHIFTED
                reason_paper = leg_overlap_conflict(
                    '.SPXW261019C7730',
                    '.SPXW261019C7755',
                    'C',
                    paper=True,
                )
                self.assertIsNotNone(reason_paper)
                self.assertIn('7755', reason_paper or '')
                out_paper = resolve_leg_overlap('261019', 'C', 7730, 7755, paper=True)
                self.assertIsNotNone(out_paper)
                ss, ls, _, _, shifts = out_paper
                self.assertEqual(shifts, 1)
                self.assertEqual((ss, ls), (7725, 7750))

    def test_live_candidate_blocks_live_active_trade(self):
        """Two live trades with overlapping legs should block each other."""
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'active')
            os.makedirs(active)
            live_path = os.path.join(active, 'live_manual.json')
            st_live = state_mod.create_new_state(
                strategy='MANUAL_SPREAD',
                lot='ms-1',
                side='C',
                short_symbol='.SPXW261019C7755',
                long_symbol='.SPXW261019C7780',
                short_strike=7755,
                long_strike=7780,
                short_fill=1.45,
                long_fill=0.85,
                net_credit=0.6,
                quantity=1,
                open_order_id='5551234',
            )
            st_live['paper'] = False
            state_mod.save_state(live_path, st_live)

            with self._patch_active_dirs(active):
                # Live candidate vs Live active: BLOCKED
                reason = leg_overlap_conflict(
                    '.SPXW261019C7730',
                    '.SPXW261019C7755',
                    'C',
                    paper=False,
                )
                self.assertIsNotNone(reason)
                self.assertIn('7755', reason or '')

                # Paper candidate vs Live active: NOT BLOCKED
                reason_paper = leg_overlap_conflict(
                    '.SPXW261019C7730',
                    '.SPXW261019C7755',
                    'C',
                    paper=True,
                )
                self.assertIsNone(reason_paper)


if __name__ == '__main__':
    unittest.main()

