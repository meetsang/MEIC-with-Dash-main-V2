"""Tests for MEIC Paper Mode Default & Manual Spread Profit Target (PT 50% default & changeable later)."""
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.handoff import apply_stop_snapshot
from blocks.session.manual_helpers import append_manual_session_row
from blocks.session.manual_place import apply_plan_metadata, plan_fields_from_request
from blocks.session.plan import SessionPlan, SessionRow, ensure_manual_session
from blocks.stop.monitor import StopMonitor
from blocks.stop.phases import PhaseAction
from blocks.stop.phases_profit_target import PhaseProfitTarget
from blocks.stop.profiles.manual import manual_stop_profile
from common.meic_settings import get_meic_settings, is_meic_paper_mode, update_meic_settings
import common.tt_config as tt_config


class TestMeicPaperMode(unittest.TestCase):
    def test_default_is_paper_mode(self):
        settings = get_meic_settings()
        self.assertTrue(settings.paper_mode)
        self.assertTrue(is_meic_paper_mode())

    def test_convert_to_real_and_back(self):
        # Switch to real
        update_meic_settings(paper_mode=False)
        self.assertFalse(is_meic_paper_mode())
        self.assertFalse(get_meic_settings().paper_mode)

        # Switch back to paper
        update_meic_settings(paper_mode=True)
        self.assertTrue(is_meic_paper_mode())
        self.assertTrue(get_meic_settings().paper_mode)


class TestManualSpreadProfitTarget(unittest.TestCase):
    def test_manual_stop_profile_includes_profit_target_phase(self):
        profile = manual_stop_profile()
        phase_types = [type(p) for p in profile.phases]
        self.assertIn(PhaseProfitTarget, phase_types)

    def test_plan_fields_defaults_pt_to_50_pct(self):
        fields = plan_fields_from_request({})
        self.assertEqual(fields['profit_target_pct'], 0.50)

        # Custom percentage given as 50 or 0.50
        fields_100 = plan_fields_from_request({'profit_target_pct': 60})
        self.assertEqual(fields_100, {**fields_100, 'profit_target_pct': 0.60})

        fields_frac = plan_fields_from_request({'profit_target_pct': 0.75})
        self.assertEqual(fields_frac['profit_target_pct'], 0.75)

    def test_apply_plan_metadata_saves_profit_target(self):
        row = SessionRow(
            slot_key='ms-01_P',
            lot='ms-01',
            side='P',
            entry_window_start='00:00',
            entry_window_end='23:59',
            profit_target_pct=0.50,
        )
        state = {}
        apply_plan_metadata(state, row)
        self.assertEqual(state['profit_target_pct'], 0.50)
        self.assertEqual(state['exit_config']['profit_target_pct'], 0.50)
        self.assertEqual(state['plan']['profit_target_pct'], 0.50)

    def test_apply_stop_snapshot_preserves_profit_target(self):
        row = SessionRow(
            slot_key='ms-02_P',
            lot='ms-02',
            side='P',
            entry_window_start='00:00',
            entry_window_end='23:59',
            stop_multiplier=2.5,
            profit_target_pct=0.60,
        )
        state = {'status': 'pending', 'filled_quantity': 1, 'quantity': 1}
        apply_stop_snapshot(state, row)
        self.assertEqual(state['status'], 'open')
        self.assertEqual(state['stop_multiplier'], 2.5)
        self.assertEqual(state['profit_target_pct'], 0.60)
        self.assertEqual(state['exit_config']['profit_target_pct'], 0.60)

    def test_phase_profit_target_activation_and_breach(self):
        phase = PhaseProfitTarget()
        monitor = MagicMock()
        monitor.stop_profile.spread_type = 'credit'
        monitor.state = {
            'status': 'open',
            'profit_target_pct': 0.50,
            'short_leg': {'symbol': 'SPX_SHORT'},
            'long_leg': {'symbol': 'SPX_LONG'},
            'entry': {'net_credit': 2.00},
        }

        # Should activate
        self.assertTrue(phase.should_activate(monitor))

        # Test mark above target (mark = 1.20 > 2.00 * (1 - 0.50) = 1.00) -> not hit
        def mock_price(sym):
            if sym == 'SPX_SHORT': return 1.50
            if sym == 'SPX_LONG': return 0.30
            return None
        monitor.prices.get_option_mid.side_effect = mock_price
        self.assertEqual(phase._evaluate_active(monitor), PhaseAction.NONE)

        # Test mark hit target (mark = 0.80 <= 1.00) -> hit
        def mock_price_hit(sym):
            if sym == 'SPX_SHORT': return 1.00
            if sym == 'SPX_LONG': return 0.20
            return None
        monitor.prices.get_option_mid.side_effect = mock_price_hit
        self.assertEqual(phase._evaluate_active(monitor), PhaseAction.EXIT_REQUIRED)

    def test_session_csv_persistence_with_pt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "test_session.csv")
            plan = SessionPlan(csv_path, strategy="MANUAL_SPREAD", rows=[
                SessionRow(
                    slot_key='ms-10_P',
                    lot='ms-10',
                    side='P',
                    entry_window_start='00:00',
                    entry_window_end='23:59',
                    stop_multiplier=2.0,
                    profit_target_pct=0.50,
                )
            ])
            plan.save()

            # Load back and verify
            loaded = SessionPlan.load(csv_path, strategy="MANUAL_SPREAD")
            row = loaded.row_by_slot_key('ms-10_P')
            self.assertIsNotNone(row)
            self.assertEqual(row.profit_target_pct, 0.50)

            # Update PT to 0.70 (e.g. 70%)
            loaded.update_row('ms-10_P', profit_target_pct=0.70)
            loaded.save()

            reloaded = SessionPlan.load(csv_path, strategy="MANUAL_SPREAD")
            row2 = reloaded.row_by_slot_key('ms-10_P')
            self.assertEqual(row2.profit_target_pct, 0.70)

    def test_check_stop_update_command_handles_pt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            trades_dir = os.path.join(tmpdir, "trades")
            commands_dir = os.path.join(trades_dir, "commands")
            os.makedirs(commands_dir, exist_ok=True)

            trade_file = os.path.join(trades_dir, "test_trade.json")
            state = {
                'status': 'open',
                'lot': 'ms-01',
                'stop_multiplier': 2.0,
                'profit_target_pct': 0.50,
            }
            with open(trade_file, 'w', encoding='utf-8') as f:
                json.dump(state, f)

            # Write command file with new PT
            cmd_file = os.path.join(commands_dir, "test_trade.json.stop_update.json")
            with open(cmd_file, 'w', encoding='utf-8') as f:
                json.dump({'profit_target_pct': 0.65}, f)

            monitor = StopMonitor.__new__(StopMonitor)
            monitor.json_path = trade_file
            monitor.state = state
            monitor.broker = MagicMock()

            # Execute _check_stop_update_command
            res = monitor._check_stop_update_command()
            self.assertTrue(res)
            self.assertEqual(monitor.state['profit_target_pct'], 0.65)
            self.assertEqual(monitor.state['exit_config']['profit_target_pct'], 0.65)
            # Command file should be unlinked
            self.assertFalse(os.path.exists(cmd_file))


if __name__ == '__main__':
    unittest.main()
