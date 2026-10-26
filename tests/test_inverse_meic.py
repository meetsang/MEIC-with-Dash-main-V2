"""Comprehensive test suite for Inverse MEIC strategy (INVERSE_MEIC_SPX)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import MagicMock, patch

from blocks.stop.phases import PhaseAction
from blocks.stop.profiles.inverse_meic import (
    INVERSE_MEIC_PROFILE,
    PhaseDebitMultipleProfitTarget,
    inverse_meic_stop_profile,
)
from common import trades_layout
from common.history_adapter import (
    is_inverse_meic_trade,
    inverse_meic_trade_to_history_row,
    production_strategy_dirs,
)
from strategies.inverse_meic.conditions import (
    MarketRegimeStatus,
    evaluate_market_conditions,
    load_regime_snapshot,
    save_regime_snapshot,
)
from strategies.inverse_meic.config import InverseMEICConfig
from strategies.inverse_meic.spread_scan import DebitSpreadCandidate, scan_debit_spread
from strategies.inverse_meic.strategy import (
    INVERSE_MEIC_TRANCHE_SLOTS,
    InverseMEICStrategy,
)


class TestInverseMEICConfig(unittest.TestCase):
    def test_default_config(self):
        cfg = InverseMEICConfig()
        self.assertEqual(cfg.name, 'INVERSE_MEIC_SPX')
        self.assertEqual(cfg.ticker, 'SPX')
        self.assertEqual(cfg.spread_width, 25)
        self.assertEqual(cfg.target_debit, 0.90)
        self.assertEqual(cfg.max_debit, 1.10)
        self.assertEqual(cfg.profit_target_multiple, 3.0)
        self.assertEqual(cfg.premarket_gap_threshold, -0.005)
        self.assertEqual(cfg.orb_range_threshold, 25.0)
        self.assertEqual(cfg.vix_threshold, 18.0)
        self.assertEqual(cfg.atr_threshold, 25.0)
        self.assertEqual(cfg.quantity, 1)
        self.assertEqual(cfg.stop_profile, INVERSE_MEIC_PROFILE)

    def test_tranche_schedule_slots(self):
        strat = InverseMEICStrategy(InverseMEICConfig())
        slots = strat.schedule()
        self.assertEqual(len(slots), 6)
        lot_names = [s.lot for s in slots]
        self.assertEqual(lot_names, ['10-30', '11-00', '12-00', '01-00', '01-15', '02-15'])


class TestMarketRegimeEvaluator(unittest.TestCase):
    def test_armed_on_premarket_gap(self):
        status = evaluate_market_conditions(
            overrides={
                'spx_spot': 5760.0,
                'spx_close': 5800.0,  # -0.69% gap (below -0.5%)
                'orb_range': 15.0,
                'vix_spot': 16.0,
                'vix_close': 16.0,
                'atr': 20.0,
            }
        )
        self.assertTrue(status.is_armed)
        self.assertEqual(status.regime, 'ARMED')
        self.assertTrue(status.premarket_gap_met)
        self.assertFalse(status.orb_range_met)
        self.assertFalse(status.vix_met)

    def test_armed_on_orb_range(self):
        status = evaluate_market_conditions(
            overrides={
                'spx_spot': 5800.0,
                'spx_close': 5800.0,
                'orb_range': 32.5,  # >= 25.0 pts
                'vix_spot': 16.0,
                'vix_close': 16.0,
                'atr': 20.0,
            }
        )
        self.assertTrue(status.is_armed)
        self.assertEqual(status.regime, 'ARMED')
        self.assertTrue(status.orb_range_met)

    def test_armed_on_spot_vix(self):
        status = evaluate_market_conditions(
            overrides={
                'spx_spot': 5800.0,
                'spx_close': 5800.0,
                'orb_range': 10.0,
                'vix_spot': 19.5,  # >= 18.0
                'vix_close': 19.0,
                'atr': 20.0,
            }
        )
        self.assertTrue(status.is_armed)
        self.assertEqual(status.regime, 'ARMED')
        self.assertTrue(status.vix_met)

    def test_standby_on_quiet_consolidation(self):
        status = evaluate_market_conditions(
            overrides={
                'spx_spot': 5805.0,
                'spx_close': 5800.0,  # +0.08%
                'orb_range': 14.0,     # < 25.0
                'vix_spot': 15.5,      # < 18.0
                'vix_close': 15.5,
                'atr': 22.0,          # < 25.0
            }
        )
        self.assertFalse(status.is_armed)
        self.assertEqual(status.regime, 'STANDBY')
        self.assertFalse(status.premarket_gap_met)
        self.assertFalse(status.orb_range_met)
        self.assertFalse(status.vix_met)
        self.assertFalse(status.atr_met)

    def test_forced_armed_override(self):
        status = evaluate_market_conditions(
            force_armed=True,
            overrides={
                'spx_spot': 5800.0,
                'spx_close': 5800.0,
                'orb_range': 10.0,
                'vix_spot': 15.0,
                'vix_close': 15.0,
                'atr': 20.0,
            }
        )
        self.assertTrue(status.is_armed)
        self.assertEqual(status.regime, 'ARMED')

    def test_save_and_load_regime_snapshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status = evaluate_market_conditions(
                root=tmpdir,
                overrides={
                    'spx_spot': 5760.0,
                    'spx_close': 5800.0,
                    'orb_range': 30.0,
                    'vix_spot': 19.0,
                    'atr': 26.0,
                }
            )
            saved_path = save_regime_snapshot(tmpdir, status)
            self.assertTrue(os.path.isfile(saved_path))
            loaded = load_regime_snapshot(tmpdir)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded['regime'], 'ARMED')
            self.assertEqual(loaded['target_multiplier'], 3.0)


class TestDebitSpreadScan(unittest.TestCase):
    def test_scan_candidate_structure(self):
        broker = MagicMock()
        # Mock broker REST option quotes
        def mock_fetch_mids(syms):
            mids = {}
            for s in syms:
                if 'C05840000' in s:
                    mids[s] = 1.40
                elif 'C05865000' in s:
                    mids[s] = 0.50
                elif 'P05760000' in s:
                    mids[s] = 1.45
                elif 'P05735000' in s:
                    mids[s] = 0.55
                else:
                    mids[s] = 0.10
            return mids
        broker.fetch_option_mids_api.side_effect = mock_fetch_mids

        # Scan Call Debit Spread (25 wide, target 0.90)
        cds = scan_debit_spread(
            broker,
            'C',
            '261026',
            5800.0,
            width=25,
            target_debit=0.90,
            otm_min=40,
            otm_max=50,
            step=5,
        )
        self.assertIsNotNone(cds)
        self.assertEqual(cds.side, 'C')
        self.assertEqual(cds.short_strike - cds.long_strike, 25)  # 25 wide
        self.assertAlmostEqual(cds.market_debit, 0.90, places=2)

        # Scan Put Debit Spread (25 wide, target 0.90)
        pds = scan_debit_spread(
            broker,
            'P',
            '261026',
            5800.0,
            width=25,
            target_debit=0.90,
            otm_min=40,
            otm_max=50,
            step=5,
        )
        self.assertIsNotNone(pds)
        self.assertEqual(pds.side, 'P')
        self.assertEqual(pds.long_strike - pds.short_strike, 25)  # 25 wide
        self.assertAlmostEqual(pds.market_debit, 0.90, places=2)


class TestInverseMEICStopProfile(unittest.TestCase):
    def test_profile_configuration(self):
        prof = inverse_meic_stop_profile()
        self.assertEqual(prof.name, INVERSE_MEIC_PROFILE)
        self.assertEqual(prof.spread_type, 'debit')
        phase_names = [p.name for p in prof.phases]
        self.assertIn('phase_debit_multiple_profit_target', phase_names)
        self.assertNotIn('phase1_initial_stop', phase_names)  # Let it bleed (no initial stop)

    def test_profit_target_multiple_exit_hit(self):
        phase = PhaseDebitMultipleProfitTarget()
        monitor = MagicMock()
        # Entry debit = $1.00, target 3.0x = $3.00
        monitor.state = {
            'status': 'open',
            'entry': {'debit': 1.00, 'side': 'C'},
            'exit_config': {'profit_target_multiple': 3.0, 'eod_exit_time_cst': '14:50'},
            'long_leg': {'symbol': 'SPX_LONG', 'strike': 5850},
            'short_leg': {'symbol': 'SPX_SHORT', 'strike': 5875},
        }
        # Mock option mids: Long = $3.80, Short = $0.70 => Spread Mark = $3.10 (>= $3.00)
        prices = MagicMock()
        prices.get_option_mid.side_effect = lambda sym: 3.80 if sym == 'SPX_LONG' else 0.70
        monitor.prices = prices

        action = phase._evaluate_active(monitor)
        self.assertEqual(action, PhaseAction.EXIT_REQUIRED)

        phase.execute(monitor)
        monitor.replace_with_spread_close.assert_called_once_with(
            reason='profit_target_multiple_hit',
            debit_limit=3.10,
        )

    def test_no_exit_when_below_target(self):
        phase = PhaseDebitMultipleProfitTarget()
        monitor = MagicMock()
        monitor.state = {
            'status': 'open',
            'entry': {'debit': 1.00, 'side': 'C'},
            'exit_config': {'profit_target_multiple': 3.0, 'eod_exit_time_cst': '14:50'},
            'long_leg': {'symbol': 'SPX_LONG', 'strike': 5850},
            'short_leg': {'symbol': 'SPX_SHORT', 'strike': 5875},
        }
        # Mark = $1.80 (below 3.0x = $3.00)
        prices = MagicMock()
        prices.get_option_mid.side_effect = lambda sym: 2.20 if sym == 'SPX_LONG' else 0.40
        monitor.prices = prices

        # Patch time to morning (e.g. 11:00 CT)
        with patch('blocks.stop.profiles.inverse_meic.central_time') as mock_ct:
            mock_ct.return_value = datetime(2026, 10, 26, 11, 0, 0)
            action = phase._evaluate_active(monitor)
            self.assertEqual(action, PhaseAction.NONE)


class TestHistoryAdapterIntegration(unittest.TestCase):
    def test_production_dirs_contains_inverse_meic(self):
        dirs = production_strategy_dirs()
        strat_names = [d[0] for d in dirs]
        self.assertIn(trades_layout.STRATEGY_INVERSE_MEIC, strat_names)

    def test_inverse_meic_history_mapping(self):
        trade_json = {
            'strategy': 'INVERSE_MEIC_SPX',
            'lot': '10-30',
            'status': 'closed',
            'quantity': 1,
            'entry': {
                'timestamp': '2026-10-26T10:30:00-05:00',
                'side': 'C',
                'debit': 0.95,
            },
            'long_leg': {'symbol': 'SPXW_L', 'fill_price': 1.55},
            'short_leg': {'symbol': 'SPXW_S', 'fill_price': 0.60},
            'close_debit': 2.85,
            'realized_pnl_dollars': 190.0,
        }
        self.assertTrue(is_inverse_meic_trade(trade_json))
        row = inverse_meic_trade_to_history_row(trade_json)
        self.assertIsNotNone(row)
        self.assertEqual(row['strategy'], 'INVERSE_MEIC_SPX')
        self.assertEqual(row['date_opened'], '2026-10-26')
        self.assertEqual(row['side'], 'C')
        self.assertEqual(row['pnl'], 190.0)
        self.assertEqual(row['open_credit'], -0.95)
        self.assertEqual(row['status'], 'CLOSED')


class TestInverseMEICStrategyEngine(unittest.TestCase):
    def test_strategy_get_status_serialization(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = InverseMEICConfig(root=tmpdir, paper=True)
            strat = InverseMEICStrategy(cfg)
            status = strat.get_status()

            self.assertEqual(status['strategy'], 'INVERSE_MEIC_SPX')
            self.assertTrue(status['paper'])
            self.assertIn('regime', status)
            self.assertIn('slots', status)
            self.assertEqual(len(status['slots']), 6)


if __name__ == '__main__':
    unittest.main()
