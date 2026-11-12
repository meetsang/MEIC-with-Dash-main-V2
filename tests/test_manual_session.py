"""Manual session CSV append and entry worker."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.manual_worker import run_manual_entry_row
from blocks.session.csv_update import apply_entry_result
from blocks.session.manual_helpers import append_manual_session_row
from blocks.session.plan import SessionPlan, load_manual_session_today
from blocks.stop.phases_profit_target import PhaseProfitTarget
from blocks.stop.profiles.manual import PhaseDebitSpreadStop
from blocks.stop.stop_math import (
    sl_pct_from_stop_multiplier,
    spread_breach_threshold,
    stop_loss_pct_for_state,
    stop_multiplier_for_state,
    stop_multiplier_from_sl_pct,
)
from blocks.stop.stop_profile import resolve_stop_profile
from common.expiry_settlement import compute_otm_decay_pnl, compute_settled_pnl, spread_intrinsic_at_expiry
from manual_spread.entry import validate_debit_spread_strikes


def _run_manual_with_mocks(tmp, row, broker, resolved_strikes=None):
    broker.get_order_status_direct = broker.get_order_status
    if resolved_strikes is None:
        resolved_strikes = (7000, 6975, '.SPXW260625P7000', '.SPXW260625P6975', 0)
    patches = [
        patch('blocks.entry.manual_worker.get_shared_broker', return_value=broker),
        patch('blocks.entry.manual_worker.effective_new_risk_blocked', return_value=False),
        patch('blocks.entry.manual_worker._resolve_strikes_for_overlap', return_value=resolved_strikes),
        patch('blocks.entry.manual_worker.register_spread_symbols'),
        patch('blocks.entry.manual_worker.util.get_expiration_date', return_value='260625'),
        patch('blocks.entry.manual_worker.state_mod.manual_spread_active_dir', return_value=tmp),
    ]
    for p in patches:
        p.start()
    try:
        return run_manual_entry_row(row)
    finally:
        for p in patches:
            p.stop()


class TestManualSession(unittest.TestCase):
    def test_append_manual_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp,
                side='P',
                short_strike=7335,
                long_strike=7310,
                limit_credit=0.90,
                quantity=3,
                expiry='2026-06-25',
            )
            self.assertEqual(row.slot_key, f'{row.lot}_P')
            self.assertEqual(row.state, 'entering')
            self.assertEqual(row.on_unfilled, 'none')
            reloaded = load_manual_session_today(tmp)
            self.assertEqual(len(reloaded.rows), 1)
            self.assertEqual(reloaded.rows[0].short_strike, 7335)

    def test_append_manual_row_with_plan_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp,
                side='P',
                short_strike=7335,
                long_strike=7310,
                limit_credit=0.90,
                quantity=3,
                expiry='2026-06-25',
                stop_multiplier=3,
                on_unfilled='chase_same_trade',
                chase_floor=0.45,
                chase_max_attempts=3,
            )
            self.assertEqual(row.stop_multiplier, 3)
            self.assertEqual(row.on_unfilled, 'chase_same_trade')
            self.assertEqual(row.credit_min, 0.45)
            self.assertEqual(row.chase1_max, 3)

    def test_manual_worker_writes_plan_to_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp, side='P', short_strike=7000, long_strike=6975, limit_credit=1.0,
                quantity=1, stop_multiplier=3,
            )
            broker = MagicMock()
            broker.place_spread_order.return_value = MagicMock(success=True, order_id='999', message='')
            broker.get_order_status.return_value = MagicMock(
                success=True, status='filled', filled_quantity=1, order_quantity=1,
                filled_price=1.0, short_fill_price=2.0, long_fill_price=1.0,
            )
            result = _run_manual_with_mocks(tmp, row, broker)
            apply_entry_result(plan.path, result, strategy=plan.strategy)
            from blocks.stop import state as state_mod
            reloaded = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved = reloaded.row_by_slot_key(row.slot_key)
            st = state_mod.load_state(saved.trade_path)
            self.assertEqual(st.get('stop_multiplier'), 3)
            self.assertEqual(st['plan']['stop_multiplier'], 3)

    def test_manual_worker_places_and_updates_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp, side='P', short_strike=7000, long_strike=6975, limit_credit=1.0, quantity=1,
            )
            broker = MagicMock()
            broker.place_spread_order.return_value = MagicMock(
                success=True, order_id='999', message='',
            )
            broker.get_order_status.return_value = MagicMock(
                success=True,
                status='filled',
                filled_quantity=1,
                order_quantity=1,
                filled_price=1.0,
                short_fill_price=2.0,
                long_fill_price=1.0,
            )
            result = _run_manual_with_mocks(tmp, row, broker)
            apply_entry_result(plan.path, result, strategy=plan.strategy)

            self.assertIn(result.api_status, ('placed', 'partial', 'working'))
            final = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved = final.row_by_slot_key(row.slot_key)
            self.assertEqual(saved.state, 'entered')
            self.assertTrue(saved.trade_path)

    def test_credit_sl_pct_conversion_and_live_trade_compat(self):
        """Verify DRMA Net Loss % convention: 200% SL = 3.0x credit stop, 100% SL = 2.0x credit stop."""
        self.assertAlmostEqual(stop_multiplier_from_sl_pct(2.0, spread_type='credit'), 3.0)
        self.assertAlmostEqual(stop_multiplier_from_sl_pct(1.0, spread_type='credit'), 2.0)
        self.assertAlmostEqual(sl_pct_from_stop_multiplier(3.0, spread_type='credit'), 2.0)

        # Simulate open ms-578_P / ms-579_C state dict (stop_multiplier=3.0, no stop_loss_pct key)
        live_credit_state = {
            'strategy': 'MANUAL_SPREAD',
            'stop_profile': 'manual_1_2',
            'stop_multiplier': 3.0,
            'profit_target_pct': 0.5,
            'entry': {
                'side': 'P',
                'net_credit': 0.85,
                'two_x_net_credit': 2.55,
            },
        }
        self.assertAlmostEqual(stop_multiplier_for_state(live_credit_state), 3.0)
        self.assertAlmostEqual(stop_loss_pct_for_state(live_credit_state), 2.0)  # 200%
        self.assertAlmostEqual(spread_breach_threshold(live_credit_state), 2.75)

    def test_debit_spread_pds_and_cds_placement_and_fractional_sl_pt_over_100(self):
        """Verify PDS and CDS strike validation, order placement, fractional SL %, and PT % > 100%."""
        # PDS: Buy Higher Put (long=6850) / Sell Lower Put (short=6825)
        self.assertIsNone(validate_debit_spread_strikes('PDS', 6825, 6850))
        self.assertIsNotNone(validate_debit_spread_strikes('PDS', 6850, 6825))

        # CDS: Buy Lower Call (long=6900) / Sell Higher Call (short=6925)
        self.assertIsNone(validate_debit_spread_strikes('CDS', 6925, 6900))
        self.assertIsNotNone(validate_debit_spread_strikes('CDS', 6900, 6925))

        with tempfile.TemporaryDirectory() as tmp:
            # Place PDS with fractional SL = 33.5% (0.335) and PT = 150% (1.50)
            plan, row = append_manual_session_row(
                tmp,
                side='PDS',
                short_strike=6825,
                long_strike=6850,
                limit_credit=2.00,
                quantity=2,
                expiry='2026-06-25',
                structure_type='debit_p',
                stop_loss_pct=0.335,
                profit_target_pct=1.50,
            )
            self.assertEqual(row.side, 'PDS')
            self.assertEqual(row.to_dict()['spread_type'], 'debit')
            self.assertAlmostEqual(row.stop_multiplier, 0.665, places=4)
            self.assertAlmostEqual(row.profit_target_pct, 1.50, places=4)

            # Verify CSV round-trip preserves fractional stop_multiplier and profit_target_pct > 1.0
            reloaded = SessionPlan.load(plan.path, strategy=plan.strategy)
            r_row = reloaded.row_by_slot_key(row.slot_key)
            self.assertAlmostEqual(r_row.stop_multiplier, 0.665, places=4)
            self.assertAlmostEqual(r_row.profit_target_pct, 1.50, places=4)

            broker = MagicMock()
            broker.place_debit_spread_order.return_value = MagicMock(
                success=True, order_id='debit-101', message='',
            )
            broker.get_order_status.return_value = MagicMock(
                success=True,
                status='filled',
                filled_quantity=2,
                order_quantity=2,
                filled_price=2.00,
                short_fill_price=3.00,
                long_fill_price=5.00,
            )
            result = _run_manual_with_mocks(
                tmp,
                r_row,
                broker,
                resolved_strikes=(6825, 6850, '.SPXW260625P6825', '.SPXW260625P6850', 0),
            )
            self.assertTrue(broker.place_debit_spread_order.called)
            apply_entry_result(plan.path, result, strategy=plan.strategy)

            from blocks.stop import state as state_mod
            final_plan = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved_row = final_plan.row_by_slot_key(row.slot_key)
            st = state_mod.load_state(saved_row.trade_path)
            self.assertEqual(st['spread_type'], 'debit')
            self.assertEqual(st['stop_profile'], 'manual_debit_spread')
            self.assertAlmostEqual(st['stop_loss_pct'], 0.335, places=4)
            self.assertAlmostEqual(st['stop_multiplier'], 0.665, places=4)
            self.assertAlmostEqual(st['profit_target_pct'], 1.50, places=4)
            self.assertAlmostEqual(st['entry']['net_debit'], 2.00, places=2)
            # two_x_net_credit stores software stop threshold for debit spread: round_spx_option_price(2.00 * 0.665) = 1.35
            self.assertAlmostEqual(st['entry']['two_x_net_credit'], 1.35, places=2)
            self.assertAlmostEqual(spread_breach_threshold(st), 1.35, places=2)

            profile = resolve_stop_profile(st)
            self.assertEqual(profile.name, 'manual_debit_spread')
            self.assertEqual(profile.spread_type, 'debit')

    def test_debit_spread_profit_target_and_software_stop_phases(self):
        """Verify PhaseProfitTarget (>100% PT) and PhaseDebitSpreadStop trigger replace_with_spread_close."""
        st = {
            'strategy': 'MANUAL_SPREAD',
            'spread_type': 'debit',
            'stop_profile': 'manual_debit_spread',
            'status': 'open',
            'stop_loss_pct': 0.50,
            'stop_multiplier': 0.50,
            'profit_target_pct': 1.50,  # +150% profit target -> target mark = 2.00 * 2.50 = 5.00
            'entry': {
                'side': 'CDS',
                'net_debit': 2.00,
                'net_credit': 2.00,
                'two_x_net_credit': 1.00,
            },
            'short_leg': {'symbol': '.SPXW260625C6925', 'strike': 6925, 'fill_price': 2.00},
            'long_leg': {'symbol': '.SPXW260625C6900', 'strike': 6900, 'fill_price': 4.00},
            'phases': {},
            'active_stop': {},
        }
        pt_phase = PhaseProfitTarget()
        sl_phase = PhaseDebitSpreadStop()

        from blocks.stop.phases import PhaseAction

        monitor = MagicMock()
        monitor.state = st
        monitor.short_sym = '.SPXW260625C6925'
        monitor.long_sym = '.SPXW260625C6900'
        monitor.prices = MagicMock(spec=['get'])

        # 1. Mark is 3.50 (long=5.00, short=1.50) -> neither PT (5.00) nor SL (1.00) hit
        monitor.prices.get.side_effect = lambda sym: 5.00 if sym.endswith('6900') else 1.50
        self.assertTrue(pt_phase.should_activate(monitor))
        self.assertTrue(sl_phase.should_activate(monitor))
        self.assertEqual(pt_phase.evaluate(monitor), PhaseAction.NONE)
        self.assertEqual(sl_phase.evaluate(monitor), PhaseAction.NONE)

        # 2. Mark rises to 5.10 (long=6.10, short=1.00) -> PT (+150% = 5.00) hit!
        monitor.prices.get.side_effect = lambda sym: 6.10 if sym.endswith('6900') else 1.00
        self.assertEqual(pt_phase.evaluate(monitor), PhaseAction.EXIT_REQUIRED)
        pt_phase.execute(monitor)
        monitor.replace_with_spread_close.assert_called_once_with(reason='profit_target_hit')

        # 3. Reset mock and test SL breach: mark drops to 0.95 (long=1.45, short=0.50) <= 1.00
        monitor.replace_with_spread_close.reset_mock()
        monitor.prices.get.side_effect = lambda sym: 1.45 if sym.endswith('6900') else 0.50
        self.assertEqual(sl_phase.evaluate(monitor), PhaseAction.EXIT_REQUIRED)
        sl_phase.execute(monitor)
        monitor.replace_with_spread_close.assert_called_once_with(reason='spread_stop_breach')

    def test_debit_spread_expiry_settlement(self):
        """Verify expiry intrinsic value and settled PnL for PDS and CDS."""
        # PDS: long 6850P, short 6825P. If SPX closes at 6800, both ITM -> intrinsic = 25.00
        self.assertAlmostEqual(spread_intrinsic_at_expiry('PDS', 6825, 6850, 6800.0), 25.00)
        # If SPX closes at 6860, both OTM -> intrinsic = 0.00
        self.assertAlmostEqual(spread_intrinsic_at_expiry('PDS', 6825, 6850, 6860.0), 0.00)

        pds_trade = {
            'strategy': 'MANUAL_SPREAD',
            'spread_type': 'debit',
            'status': 'closed',
            'quantity': 1,
            'filled_quantity': 1,
            'short_close_price': 1.00,
            'long_close_price': 4.50,
            'entry': {'side': 'PDS', 'net_debit': 2.00, 'timestamp': '2026-06-25T10:00:00'},
            'short_leg': {'symbol': '.SPXW260625P6825', 'strike': 6825, 'fill_price': 2.00},
            'long_leg': {'symbol': '.SPXW260625P6850', 'strike': 6850, 'fill_price': 4.00},
        }
        settled = compute_settled_pnl(pds_trade, 6840.0)
        self.assertEqual(settled['close_debit'], 3.50)
        self.assertEqual(settled['pnl'], 150.00)  # (3.50 - 2.00) * 100 * 1

        otm = compute_otm_decay_pnl(pds_trade)
        self.assertEqual(otm['pnl'], -200.00)

    def test_step3_credit_vs_debit_placement_and_strike_ordering(self):
        """Verify Put/Call Spread placement as Credit or Debit and strike ordering."""
        def format_strikes(k1, k2, side_or_type):
            s = str(side_or_type).upper()
            is_put = '_P' in s or s.endswith('P') or s.startswith('P') or 'PUT' in s
            is_call = not is_put and ('_C' in s or s.endswith('C') or s.startswith('C') or 'CALL' in s)
            if is_call:
                return f'{min(k1, k2)} / {max(k1, k2)}'
            return f'{max(k1, k2)} / {min(k1, k2)}'

        # Calls: smaller strike appears first regardless of credit or debit
        self.assertEqual(format_strikes(5925, 5900, 'C'), '5900 / 5925')
        self.assertEqual(format_strikes(5900, 5925, 'vertical_c'), '5900 / 5925')
        self.assertEqual(format_strikes(5925, 5900, 'CDS'), '5900 / 5925')
        self.assertEqual(format_strikes(5900, 5925, 'CCS'), '5900 / 5925')

        # Puts: higher strike appears first regardless of credit or debit
        self.assertEqual(format_strikes(5775, 5800, 'P'), '5800 / 5775')
        self.assertEqual(format_strikes(5800, 5775, 'vertical_p'), '5800 / 5775')
        self.assertEqual(format_strikes(5775, 5800, 'PDS'), '5800 / 5775')
        self.assertEqual(format_strikes(5800, 5775, 'PCS'), '5800 / 5775')

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Put Spread as Credit (PCS): sell 5800, buy 5775
            plan_p_cred, row_p_cred = append_manual_session_row(
                tmp,
                side='P',
                short_strike=5800,
                long_strike=5775,
                limit_credit=0.60,
                structure_type='vertical_p',
            )
            self.assertEqual(row_p_cred.side, 'P')
            self.assertEqual(row_p_cred.short_strike, 5800)
            self.assertEqual(row_p_cred.long_strike, 5775)
            self.assertEqual(row_p_cred.to_dict()['spread_type'], 'credit')

            # 2. Put Spread as Debit (PDS): sell 5775, buy 5800
            plan_p_deb, row_p_deb = append_manual_session_row(
                tmp,
                side='PDS',
                short_strike=5775,
                long_strike=5800,
                limit_credit=0.60,
                structure_type='debit_p',
            )
            self.assertEqual(row_p_deb.side, 'PDS')
            self.assertEqual(row_p_deb.short_strike, 5775)
            self.assertEqual(row_p_deb.long_strike, 5800)
            self.assertEqual(row_p_deb.to_dict()['spread_type'], 'debit')

            # 3. Call Spread as Credit (CCS): sell 5900, buy 5925
            plan_c_cred, row_c_cred = append_manual_session_row(
                tmp,
                side='C',
                short_strike=5900,
                long_strike=5925,
                limit_credit=0.60,
                structure_type='vertical_c',
            )
            self.assertEqual(row_c_cred.side, 'C')
            self.assertEqual(row_c_cred.short_strike, 5900)
            self.assertEqual(row_c_cred.long_strike, 5925)
            self.assertEqual(row_c_cred.to_dict()['spread_type'], 'credit')

            # 4. Call Spread as Debit (CDS): sell 5925, buy 5900
            plan_c_deb, row_c_deb = append_manual_session_row(
                tmp,
                side='CDS',
                short_strike=5925,
                long_strike=5900,
                limit_credit=0.60,
                structure_type='debit_c',
            )
            self.assertEqual(row_c_deb.side, 'CDS')
            self.assertEqual(row_c_deb.short_strike, 5925)
            self.assertEqual(row_c_deb.long_strike, 5900)
            self.assertEqual(row_c_deb.to_dict()['spread_type'], 'debit')


if __name__ == '__main__':
    unittest.main()
