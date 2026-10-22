"""Comprehensive test suite for DRMA + Hilega-Milega (DRMA_HM) Strategy."""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd

from common.standalone_runner import StandaloneStrategyRunner
from strategies.drma_hm.config import DRMAHMConfig
from strategies.drma_hm.indicators import (
    IndicatorSnapshot,
    compute_indicators,
    evaluate_exit_signal,
    rsi_wilder,
    wma_series,
)
from strategies.drma_hm.legs import (
    build_closing_legs,
    build_credit_spread_legs,
    build_debit_spread_legs,
    estimate_0dte_delta,
    estimate_option_price,
    round_atm,
    select_directional_strikes,
    select_non_directional_strikes,
)
from strategies.drma_hm.market_data_feed import (
    fetch_es_current_price,
    fetch_yahoo_market_levels,
    load_spx_5m_bars,
)
from strategies.drma_hm.settlement import (
    attach_dollar_pnl_display,
    pnl_points_to_dollars,
    settle_open_trade,
    update_futures_unrealized_marks,
    update_unrealized_marks,
)
from strategies.drma_hm.strategy import DRMAHMStrategy


class TestDRMAHMIndicators(unittest.TestCase):
    def test_round_atm(self):
        self.assertEqual(round_atm(7782.4, 5), 7780)
        self.assertEqual(round_atm(7783.0, 5), 7785)
        self.assertEqual(round_atm(7800.0, 5), 7800)

    def test_rsi_and_wma(self):
        # Monotonically increasing closes -> RSI near 100
        closes = pd.Series([100.0 + i * 2.0 for i in range(30)])
        rsi = rsi_wilder(closes, period=9)
        self.assertGreater(rsi.iloc[-1], 80.0)

        # Monotonically decreasing closes -> RSI near 0
        closes_dn = pd.Series([200.0 - i * 2.0 for i in range(30)])
        rsi_dn = rsi_wilder(closes_dn, period=9)
        self.assertLess(rsi_dn.iloc[-1], 20.0)

        # WMA of constant series equals constant
        const_s = pd.Series([50.0] * 25)
        wma = wma_series(const_s, period=21)
        self.assertAlmostEqual(wma.iloc[-1], 50.0, places=4)

    def test_daily_reset_ma_and_orb(self):
        # Construct synthetic 10 bars (each 5 min starting at 08:30)
        base_t = datetime(2026, 10, 8, 8, 30)
        dts = [(base_t + timedelta(minutes=5 * i)).strftime('%Y-%m-%d %H:%M:%S') for i in range(10)]
        df = pd.DataFrame({
            'datetime': dts,
            'open': [7800.0 + i for i in range(10)],
            'high': [7805.0 + i for i in range(10)],
            'low': [7795.0 + i for i in range(10)],
            'close': [7801.0 + i for i in range(10)],
            'is_today': [True] * 10,
        })

        df_calc, snaps = compute_indicators(
            df,
            ma_type='EMA',
            ma_len=21,
            orb_minutes=30,  # First 6 bars: 08:30 to 08:55
            pdh=7815.0,
            pdl=7760.0,
            pdc=7800.0,
            onh=7810.0,
            onl=7770.0,
        )

        self.assertEqual(len(snaps), 10)
        # Bar 0 should initialize MA to close
        self.assertEqual(snaps[0].out_ma, 7801.0)
        # Bar 5 (08:55): ORB should not be formed yet (< 30 min completion)
        self.assertFalse(snaps[5].orb_formed)
        # Bar 6 (09:00): ORB should be marked formed
        self.assertTrue(snaps[6].orb_formed)
        self.assertIsNotNone(snaps[6].orb_high)
        self.assertIsNotNone(snaps[6].orb_low)
        self.assertAlmostEqual(snaps[6].orb_mid, (snaps[6].orb_high + snaps[6].orb_low) / 2.0, places=2)

    def test_exit_signal_evaluation(self):
        # Create mock snapshots
        snap_prev = IndicatorSnapshot(
            bar_index=10, datetime_str='2026-10-08 09:20:00',
            close=7790.0, open=7785.0, high=7792.0, low=7784.0,
            out_ma=7785.0, ma_delta_2=1.0, ma_slope_up=True, ma_slope_down=False,
            is_not_extended=True, orb_high=7795.0, orb_low=7770.0, orb_mid=7782.5,
            orb_formed=True, pdh=7800.0, pdl=7760.0, pdc=7780.0, onh=7795.0, onl=7775.0,
            rsi=55.0, hm_fast=54.0, hm_slow=52.0,
            is_super_bull=True, is_super_bear=False, is_bull_pullback=False, is_bear_bounce=False,
            bull_score=5, bear_score=0, active_score=5, bias='BULL',
            setup_grade='GRADE A+ (5/5)', trade_vehicle='BUY CALLS / CDS', gamma_risk='NORMAL / THETA SAFE',
        )

        # Reversal snapshot: close below MA, fast hooks below slow
        snap_curr = IndicatorSnapshot(
            bar_index=11, datetime_str='2026-10-08 09:25:00',
            close=7780.0, open=7790.0, high=7790.0, low=7778.0,
            out_ma=7784.0, ma_delta_2=-1.0, ma_slope_up=False, ma_slope_down=True,
            is_not_extended=True, orb_high=7795.0, orb_low=7770.0, orb_mid=7782.5,
            orb_formed=True, pdh=7800.0, pdl=7760.0, pdc=7780.0, onh=7795.0, onl=7775.0,
            rsi=42.0, hm_fast=48.0, hm_slow=51.0,
            is_super_bull=False, is_super_bear=False, is_bull_pullback=True, is_bear_bounce=False,
            bull_score=1, bear_score=3, active_score=3, bias='BEAR',
            setup_grade='GRADE B (3/5)', trade_vehicle='SELL CCS', gamma_risk='NORMAL / THETA SAFE',
        )

        # Held bars less than min_hold_bars (e.g. entry at bar 10, current is 11) -> hold
        exit_sig, reason = evaluate_exit_signal('LONG', [snap_prev, snap_curr], min_hold_bars=3, entry_bar_index=10)
        self.assertFalse(exit_sig)

        # Held bars >= 3 (e.g. entry at bar 5)
        exit_sig_2, reason_2 = evaluate_exit_signal('LONG', [snap_prev, snap_curr], exit_style='trend_runner', min_hold_bars=3, entry_bar_index=5)
        self.assertTrue(exit_sig_2)
        self.assertIn('H-M Fast crossed under Slow', reason_2)


class TestDRMAHMLegs(unittest.TestCase):
    def test_delta_and_price_estimates(self):
        spot = 7800.0
        # ATM call delta roughly ~0.50
        atm_c_delta = estimate_0dte_delta(spot, 7800.0, 'C', iv=0.18, dte_hours=5.0)
        self.assertAlmostEqual(atm_c_delta, 0.50, delta=0.08)

        # OTM call (strike 7825) should have delta between 0.10 and 0.35
        otm_c_delta = estimate_0dte_delta(spot, 7825.0, 'C', iv=0.18, dte_hours=5.0)
        self.assertTrue(0.08 <= otm_c_delta <= 0.35)

        # OTM put (strike 7775) should have delta between -0.35 and -0.08
        otm_p_delta = estimate_0dte_delta(spot, 7775.0, 'P', iv=0.18, dte_hours=5.0)
        self.assertTrue(-0.35 <= otm_p_delta <= -0.08)

        # Price estimate
        atm_price = estimate_option_price(spot, 7800.0, 'C', iv=0.18, dte_hours=5.0)
        self.assertGreater(atm_price, 5.0)

    def test_select_credit_spread_by_delta(self):
        spot = 7780.0
        # Bull Put Spread (PCS): short strike below spot
        short_k, long_k, delta, credit = select_non_directional_strikes(
            side='P',
            spot=spot,
            spread_width=10,
            selection_mode='delta',
            target_delta=0.15,
            min_delta=0.08,
            max_delta=0.25,
            expiry_yymmdd='261008',
        )
        self.assertLess(short_k, spot)
        self.assertEqual(long_k, short_k - 10)
        self.assertTrue(-0.25 <= delta <= -0.08)
        self.assertGreater(credit, 0.20)

        # Bear Call Spread (CCS): short strike above spot
        short_k_c, long_k_c, delta_c, credit_c = select_non_directional_strikes(
            side='C',
            spot=spot,
            spread_width=10,
            selection_mode='delta',
            target_delta=0.15,
            min_delta=0.08,
            max_delta=0.25,
            expiry_yymmdd='261008',
        )
        self.assertGreater(short_k_c, spot)
        self.assertEqual(long_k_c, short_k_c + 10)
        self.assertTrue(0.08 <= delta_c <= 0.25)
        self.assertGreater(credit_c, 0.20)

    def test_select_credit_spread_by_credit(self):
        spot = 7780.0
        short_k, long_k, delta, credit = select_non_directional_strikes(
            side='P',
            spot=spot,
            spread_width=10,
            selection_mode='credit',
            target_credit=1.00,
            min_credit=0.40,
            expiry_yymmdd='261008',
        )
        self.assertLess(short_k, spot)
        self.assertEqual(long_k, short_k - 10)
        self.assertAlmostEqual(credit, 1.00, delta=0.60)

    def test_select_directional_spread_by_delta(self):
        spot = 7780.0
        # Bull Call Debit Spread: long call near ATM / delta ~ 0.40
        long_k, short_k, delta, debit = select_directional_strikes(
            side='C',
            spot=spot,
            spread_width=10,
            selection_mode='delta',
            target_delta=0.40,
            min_delta=0.25,
            max_delta=0.55,
            expiry_yymmdd='261008',
        )
        self.assertEqual(short_k, long_k + 10)
        self.assertTrue(0.25 <= delta <= 0.55)
        self.assertGreater(debit, 0.50)

    def test_select_directional_spread_by_debit(self):
        spot = 7780.0
        long_k, short_k, delta, debit = select_directional_strikes(
            side='P',
            spot=spot,
            spread_width=10,
            selection_mode='debit',
            target_debit=2.50,
            max_debit=4.00,
            expiry_yymmdd='261008',
        )
        self.assertEqual(short_k, long_k - 10)
        self.assertLessEqual(debit, 4.00)

    def test_build_legs_and_closing(self):
        c_legs = build_credit_spread_legs(7760, 7750, 'P', '261008', quantity=2)
        self.assertEqual(len(c_legs), 2)
        self.assertEqual(c_legs[0].action, 'SELL_TO_OPEN')
        self.assertEqual(c_legs[1].action, 'BUY_TO_OPEN')
        self.assertEqual(c_legs[0].quantity, 2)

        closing = build_closing_legs(c_legs, quantity=2)
        self.assertEqual(len(closing), 2)
        self.assertEqual(closing[0].action, 'BUY_TO_CLOSE')
        self.assertEqual(closing[1].action, 'SELL_TO_CLOSE')


class TestSettlement(unittest.TestCase):
    def test_pnl_points_to_dollars(self):
        self.assertEqual(pnl_points_to_dollars(1.50, quantity=1), 150.0)
        self.assertEqual(pnl_points_to_dollars(-0.75, quantity=2), -150.0)

    def test_credit_spread_unrealized_marks(self):
        trade = {
            'quantity': 1,
            'structure_type': 'credit_spread',
            'entry_price': 1.20,
            'structures': [{
                'structure_id': 's1',
                'net_credit': 1.20,
                'legs': [
                    {'symbol': 'SPXW  261008P07760000', 'action': 'SELL_TO_OPEN'},
                    {'symbol': 'SPXW  261008P07750000', 'action': 'BUY_TO_OPEN'},
                ],
            }],
        }
        # Option mids: short mid = 0.50, long mid = 0.10 -> mark = 0.40
        # PnL = 1.20 - 0.40 = +0.80 points = +$80.00
        mids = {
            'SPXW  261008P07760000': 0.50,
            'SPXW  261008P07750000': 0.10,
        }
        unrealized = update_unrealized_marks(trade, mids)
        self.assertAlmostEqual(unrealized, 0.80, places=2)
        self.assertEqual(trade['unrealized_pnl_dollars'], 80.0)

    def test_debit_spread_unrealized_marks(self):
        trade = {
            'quantity': 1,
            'structure_type': 'debit_spread',
            'entry_price': 2.50,
            'structures': [{
                'structure_id': 's1',
                'net_debit': 2.50,
                'legs': [
                    {'symbol': 'SPXW  261008C07780000', 'action': 'BUY_TO_OPEN'},
                    {'symbol': 'SPXW  261008C07790000', 'action': 'SELL_TO_OPEN'},
                ],
            }],
        }
        # Option mids: long mid = 4.00, short mid = 1.00 -> mark = 3.00
        # PnL = 3.00 - 2.50 = +0.50 points = +$50.00
        mids = {
            'SPXW  261008C07780000': 4.00,
            'SPXW  261008C07790000': 1.00,
        }
        unrealized = update_unrealized_marks(trade, mids)
        self.assertAlmostEqual(unrealized, 0.50, places=2)
        self.assertEqual(trade['unrealized_pnl_dollars'], 50.0)


class TestDRMAHMIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = DRMAHMConfig(root=self.tmp, paper=True, confirmed=True)
        self.strategy = DRMAHMStrategy(self.cfg)

    def test_manual_levels_priority(self):
        cfg = DRMAHMConfig(
            enable_yahoo_finance=False,
            manual_pdh=7820.0,
            manual_pdl=7750.0,
            manual_pdc=7805.0,
            manual_onh=7818.0,
            manual_onl=7762.0,
        )
        levels = fetch_yahoo_market_levels(date(2026, 10, 8), cfg, self.tmp)
        self.assertEqual(levels['pdh'], 7820.0)
        self.assertEqual(levels['pdl'], 7750.0)
        self.assertEqual(levels['pdc'], 7805.0)
        self.assertEqual(levels['onh'], 7818.0)
        self.assertEqual(levels['onl'], 7762.0)
        self.assertEqual(levels['source'], 'manual')

    def test_standalone_runner_wrapping(self):
        runner = StandaloneStrategyRunner(self.strategy, root=self.tmp)
        self.assertFalse(runner._running)
        self.assertEqual(runner.config.name, 'DRMA_HM_SPX')
        self.assertTrue(runner.config.paper)

    def test_backtest_simulation_on_recorded_csv(self):
        """Simulate indicator processing on user's data/2026-10-08/SPX_5m.csv."""
        workspace_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        data_root = os.path.join(workspace_root, 'data')
        oct_8 = date(2026, 10, 8)

        df = load_spx_5m_bars(data_root, oct_8, prepend_prev_bars=40)
        self.assertFalse(df.empty)
        self.assertIn('close', df.columns)

        df_calc, snaps = compute_indicators(
            df,
            ma_type='EMA',
            ma_len=21,
            orb_minutes=30,
            min_slope_pts=0.5,
            max_extension_pts=18.0,
            pdh=7814.77,
            pdl=7763.34,
            pdc=7800.26,
            onh=7804.51,
            onl=7749.01,
        )

        today_snaps = [s for s, is_t in zip(snaps, df['is_today']) if is_t]
        self.assertEqual(len(today_snaps), 78)

        # Check ORB formation after bar 6
        self.assertTrue(today_snaps[6].orb_formed)
        self.assertAlmostEqual(today_snaps[6].orb_high, 7801.72, places=1)
        self.assertAlmostEqual(today_snaps[6].orb_low, 7771.75, places=1)

        # Find signals that triggered during Oct 8 (with wait_for_orb=True)
        bear_signals = [s for s in today_snaps if s.orb_formed and s.bear_score >= 4 and s.close < s.out_ma and s.ma_slope_down]
        self.assertGreater(len(bear_signals), 0)

        # First bear signal fired around 09:50 Central as price lost ORB Low and trend broke down
        first_bear = bear_signals[0]
        self.assertIn('09:50', first_bear.datetime_str)
        self.assertEqual(first_bear.bias, 'BEAR')


class TestDRMAHMFutures(unittest.TestCase):
    @staticmethod
    def _make_snap(bias='BULL', close=5800.0, score=5):
        grade = 'GRADE A+ (5/5)' if score == 5 else ('GRADE A (4/5)' if score == 4 else 'GRADE B (3/5)')
        return IndicatorSnapshot(
            bar_index=15, datetime_str='2026-10-15 09:30:00',
            close=close, open=5795.0, high=5802.0, low=5794.0,
            out_ma=5790.0, ma_delta_2=1.0, ma_slope_up=True, ma_slope_down=False,
            is_not_extended=True, orb_high=5810.0, orb_low=5780.0, orb_mid=5795.0,
            orb_formed=True, pdh=5820.0, pdl=5770.0, pdc=5795.0, onh=5815.0, onl=5785.0,
            rsi=60.0, hm_fast=58.0, hm_slow=54.0,
            is_super_bull=(bias == 'BULL'), is_super_bear=(bias == 'BEAR'),
            is_bull_pullback=False, is_bear_bounce=False,
            bull_score=score if bias == 'BULL' else 0,
            bear_score=0 if bias == 'BULL' else score,
            active_score=score, bias=bias,
            setup_grade=grade,
            trade_vehicle='BUY CALLS / CDS' if score == 5 else 'SELL PCS',
            gamma_risk='NORMAL / THETA SAFE',
        )

    def test_trade_mode_properties(self):
        cfg = DRMAHMConfig(trade_mode='all_three')
        self.assertTrue(cfg.is_directional_futures_enabled)
        self.assertTrue(cfg.is_directional_options_enabled)
        self.assertTrue(cfg.is_non_directional_options_enabled)

        cfg_fut = DRMAHMConfig(trade_mode='directional_futures')
        self.assertTrue(cfg_fut.is_directional_futures_enabled)
        self.assertFalse(cfg_fut.is_directional_options_enabled)
        self.assertFalse(cfg_fut.is_non_directional_options_enabled)

        cfg_both = DRMAHMConfig(trade_mode='both')
        self.assertFalse(cfg_both.is_directional_futures_enabled)
        self.assertTrue(cfg_both.is_directional_options_enabled)
        self.assertTrue(cfg_both.is_non_directional_options_enabled)

        cfg_dir_both = DRMAHMConfig(trade_mode='directional_both')
        self.assertTrue(cfg_dir_both.is_directional_futures_enabled)
        self.assertTrue(cfg_dir_both.is_directional_options_enabled)
        self.assertFalse(cfg_dir_both.is_non_directional_options_enabled)

    def test_fetch_es_current_price_fallback(self):
        with patch('requests.get', side_effect=Exception('Network error')):
            price = fetch_es_current_price(None, levels={'es_offset': 5.0}, spx_spot=5850.0)
            self.assertEqual(price, 5855.0)

            price2 = fetch_es_current_price(None, es_basis=4.5, spx_spot=5850.0)
            self.assertEqual(price2, 5854.5)

    def test_futures_marks_and_settlement(self):
        # Long trade: 1 contract of ES entered at 5800.0, mark moves to 5802.5 (+2.5 pts)
        long_trade = {
            'structure_type': 'futures',
            'trade_type': 'FUTURES',
            'side': 'LONG',
            'symbol': 'ES',
            'quantity': 1,
            'multiplier': 50.0,
            'entry_price': 5800.0,
        }
        unrealized_pts = update_futures_unrealized_marks(long_trade, 5802.5)
        self.assertAlmostEqual(unrealized_pts, 2.5)
        # 2.5 points * $50/pt * 1 qty = $125.00
        self.assertAlmostEqual(long_trade['unrealized_pnl_dollars'], 125.0)

        # Short trade: 2 contracts of ES entered at 5800.0, mark moves to 5797.0 (+3.0 pts gain)
        short_trade = {
            'structure_type': 'futures',
            'trade_type': 'FUTURES',
            'side': 'SHORT',
            'symbol': 'ES',
            'quantity': 2,
            'multiplier': 50.0,
            'entry_price': 5800.0,
        }
        unrealized_pts_short = update_futures_unrealized_marks(short_trade, 5797.0)
        self.assertAlmostEqual(unrealized_pts_short, 3.0)
        # 3.0 points * $50/pt * 2 qty = $300.00
        self.assertAlmostEqual(short_trade['unrealized_pnl_dollars'], 300.0)

    def test_futures_profit_target_and_stop_loss_trigger(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            trade_mode='directional_futures',
            futures_symbol='ES',
            futures_profit_target_pts=5.0,
            futures_stop_loss_pts=5.0,
        )
        strat = DRMAHMStrategy(cfg)
        snap = self._make_snap('BULL', 5800.0)

        # Test trade entry
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            trade = strat._enter_directional_futures_trade(
                root=tmp,
                expiry_yymmdd='261015',
                snap=snap,
                bar_idx=15,
            )
        self.assertIsNotNone(trade)
        self.assertEqual(trade['structure_type'], 'futures')
        self.assertEqual(trade['trade_type'], 'DIRECTIONAL_FUTURES')
        self.assertEqual(trade['entry_price'], 5800.0)
        self.assertEqual(trade['profit_target_price'], 5805.0)
        self.assertEqual(trade['stop_loss_price'], 5795.0)
        self.assertEqual(trade['multiplier'], 50.0)
        self.assertTrue(trade['paper'])
        self.assertEqual(strat._futures_position_state, 1)

        # Manage open trade: within bounds (5802.0) -> stays open
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5802.0):
            strat._manage_open_trade(tmp, strat._active_trade_path, snapshots=[snap])
        self.assertEqual(strat._futures_position_state, 1)

        # Manage open trade: hits profit target (5805.25) -> exits with profit
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5805.25):
            strat._manage_open_trade(tmp, strat._active_trade_path, snapshots=[snap])
        self.assertEqual(strat._futures_position_state, 0)
        self.assertIsNone(strat._active_trade_path)

    def test_futures_stop_loss_trigger(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            trade_mode='directional_futures',
            futures_profit_target_pts=5.0,
            futures_stop_loss_pts=5.0,
        )
        strat = DRMAHMStrategy(cfg)
        snap = self._make_snap('BULL', 5800.0)

        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            strat._enter_directional_futures_trade(tmp, '261015', snap, 15)

        self.assertEqual(strat._futures_position_state, 1)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5794.75):
            strat._manage_open_trade(tmp, strat._active_trade_path, snapshots=[snap])

        self.assertEqual(strat._futures_position_state, 0)
        self.assertIsNone(strat._active_trade_path)

    def test_dual_state_independence(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(root=tmp, paper=True, trade_mode='all_three')
        strat = DRMAHMStrategy(cfg)

        self.assertEqual(strat._options_position_state, 0)
        self.assertEqual(strat._futures_position_state, 0)

        snap = self._make_snap('BULL', 5800.0)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            strat._enter_directional_futures_trade(tmp, '261015', snap, 10)

        self.assertEqual(strat._futures_position_state, 1)
        # Options state remains flat and ready to trade concurrently in all_three mode
        self.assertEqual(strat._options_position_state, 0)

    def test_paper_isolation_never_sends_broker_orders(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(root=tmp, paper=True, trade_mode='directional_futures')
        strat = DRMAHMStrategy(cfg)
        mock_broker = MagicMock()
        strat.broker = mock_broker

        snap = self._make_snap('BULL', 5800.0)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            trade = strat._enter_directional_futures_trade(tmp, '261015', snap, 1)

        mock_broker.place_market_order.assert_not_called()
        mock_broker.place_order.assert_not_called()
        self.assertTrue(trade['paper'])

        trade_path = strat._active_trade_path
        strat._exit_trade(tmp, trade_path, reason='manual_close')
        mock_broker.place_market_order.assert_not_called()
        mock_broker.place_order.assert_not_called()

    def test_live_wiring_sends_broker_orders(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(root=tmp, paper=False, trade_mode='directional_futures')
        strat = DRMAHMStrategy(cfg)
        mock_broker = MagicMock()
        mock_broker.place_market_order.return_value = MagicMock(success=True, filled_price=5800.0, order_id='fut_order_1')
        strat.broker = mock_broker

        snap = self._make_snap('BULL', 5800.0)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            trade = strat._enter_directional_futures_trade(tmp, '261015', snap, 1)

        mock_broker.place_market_order.assert_called_with('BUY_TO_OPEN', 'ES', 1)
        self.assertFalse(trade['paper'])

        trade_path = strat._active_trade_path
        strat._exit_trade(tmp, trade_path, reason='profit_target_hit')
        mock_broker.place_market_order.assert_called_with('SELL_TO_CLOSE', 'ES', 1)

    def test_futures_triggers_on_a_plus_only(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            trade_mode='all_three',
            min_grade_to_signal='Grade A and A+',
            futures_min_grade='Grade A+ Only',
        )
        strat = DRMAHMStrategy(cfg)
        mock_broker = MagicMock()
        mock_broker.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.0)
        strat.broker = mock_broker

        # 1. Test Grade A setup (score 4/5): Options enters Credit Spread, Futures does NOT enter
        snap_a = self._make_snap('BULL', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0), \
             patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5790, 5780, 0.15, 1.0)):
            strat._evaluate_and_enter(tmp, '261015', snap_a, 1)

        self.assertEqual(strat._options_position_state, 1)  # Entered Credit Spread
        self.assertEqual(strat._futures_position_state, 0)  # Futures BLOCKED because score is 4 (not A+)

        # Reset states for A+ test
        strat._options_position_state = 0
        strat._futures_position_state = 0

        # 2. Test Grade A+ setup (score 5/5): Futures DOES enter
        snap_aplus = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0), \
             patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_aplus, 2)

        self.assertEqual(strat._futures_position_state, 1)  # Futures ENTERED on Grade A+ (score 5)

    def test_futures_backward_compatibility_grade_a(self):
        tmp = tempfile.mkdtemp()
        # If user explicitly sets futures_min_grade to 'Grade A and A+', score 4 triggers futures
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            trade_mode='all_three',
            min_grade_to_signal='Grade A and A+',
            futures_min_grade='Grade A and A+',
        )
        strat = DRMAHMStrategy(cfg)
        mock_broker = MagicMock()
        mock_broker.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.0)
        strat.broker = mock_broker

        snap_a = self._make_snap('BULL', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0), \
             patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5790, 5780, 0.15, 1.0)):
            strat._evaluate_and_enter(tmp, '261015', snap_a, 1)

        self.assertEqual(strat._futures_position_state, 1)  # Futures ENTERED because min grade allows Grade A

    def test_decoupled_options_concurrent_entry(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            trade_mode='all_three',
            min_grade_to_signal='Grade A and A+',
            futures_min_grade='Grade A+ Only',
            dir_block_if_same_side_nondir=False,
        )
        strat = DRMAHMStrategy(cfg)
        mock_broker = MagicMock()
        mock_broker.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.0)
        strat.broker = mock_broker

        # Step 1: Grade A setup (score 4) enters Credit Spread
        snap_a = self._make_snap('BEAR', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0), \
             patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5810, 5820, 0.15, 1.0)):
            strat._evaluate_and_enter(tmp, '261015', snap_a, 1)

        self.assertEqual(strat._credit_options_position_state, -1)  # Credit Spread active
        self.assertEqual(strat._dir_options_position_state, 0)     # Directional options slot is FLAT
        self.assertEqual(strat._futures_position_state, 0)         # Futures slot is FLAT

        # Step 2: While Credit Spread is still open, Grade A+ setup (score 5) fires!
        # Because slots are decoupled, Directional Debit Spread and Directional Futures BOTH enter!
        snap_aplus = self._make_snap('BEAR', 5790.0, score=5)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5790.0), \
             patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5790, 5780, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_aplus, 2)

        self.assertEqual(strat._credit_options_position_state, -1)  # Credit Spread remains active
        self.assertEqual(strat._dir_options_position_state, -1)    # Debit Spread entered successfully!
        self.assertEqual(strat._futures_position_state, -1)        # Futures entered successfully!
        self.assertEqual(strat._position_state, 1)

    def test_independent_sub_strategy_config_inheritance_and_overrides(self):
        # 1. Defaults inherit global paper setting
        cfg_paper = DRMAHMConfig(paper=True)
        self.assertTrue(cfg_paper.is_dir_paper)
        self.assertTrue(cfg_paper.is_non_dir_paper)
        self.assertTrue(cfg_paper.is_futures_paper)
        self.assertFalse(cfg_paper.has_any_live_sub_strategy)

        cfg_live = DRMAHMConfig(paper=False)
        self.assertFalse(cfg_live.is_dir_paper)
        self.assertFalse(cfg_live.is_non_dir_paper)
        self.assertFalse(cfg_live.is_futures_paper)
        self.assertTrue(cfg_live.has_any_live_sub_strategy)

        # 2. Independent overrides override global paper
        cfg_mixed_fut_live = DRMAHMConfig(paper=True, futures_paper=False)
        self.assertTrue(cfg_mixed_fut_live.is_dir_paper)
        self.assertTrue(cfg_mixed_fut_live.is_non_dir_paper)
        self.assertFalse(cfg_mixed_fut_live.is_futures_paper)
        self.assertTrue(cfg_mixed_fut_live.has_any_live_sub_strategy)

        cfg_mixed_options_live = DRMAHMConfig(paper=True, dir_paper=False, non_dir_paper=False, futures_paper=True)
        self.assertFalse(cfg_mixed_options_live.is_dir_paper)
        self.assertFalse(cfg_mixed_options_live.is_non_dir_paper)
        self.assertTrue(cfg_mixed_options_live.is_futures_paper)
        self.assertTrue(cfg_mixed_options_live.has_any_live_sub_strategy)

        # 3. is_sub_strategy_paper resolver helper
        self.assertFalse(cfg_mixed_fut_live.is_sub_strategy_paper('DRMA_HM_FUT'))
        self.assertTrue(cfg_mixed_fut_live.is_sub_strategy_paper('DRMA_HM_DIR'))
        self.assertTrue(cfg_mixed_fut_live.is_sub_strategy_paper('DRMA_HM_NONDIR'))
        self.assertTrue(cfg_mixed_fut_live.is_sub_strategy_paper('credit_spread'))
        self.assertFalse(cfg_mixed_fut_live.is_sub_strategy_paper('es_futures'))

    def test_mixed_mode_routing_futures_live_options_paper(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            futures_paper=False,  # Futures live, Options paper
            trade_mode='all_three',
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock()
        mock_paper._is_journal = True
        mock_paper._is_paper = True
        mock_paper.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.5, order_id='opt_paper_123')

        mock_live = MagicMock()
        mock_live._is_journal = False
        mock_live._is_paper = False
        mock_live.place_market_order.return_value = MagicMock(success=True, filled_price=5800.0, order_id='fut_live_123')
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.5, order_id='opt_live_123')

        strat._paper_broker = mock_paper
        strat._live_broker = mock_live

        snap = self._make_snap('BULL', 5800.0, score=5)

        # 1. Directional Options Spread entry (Paper mode)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat._enter_directional_trade(tmp, '261015', snap, 1)

        dir_trade_path = strat._active_trade_path
        with open(dir_trade_path, encoding='utf-8') as f:
            dir_trade = json.load(f)

        self.assertTrue(dir_trade['paper'])
        self.assertEqual(dir_trade.get('sub_strategy'), 'DRMA_HM_DIR')
        # Live broker was NOT called for paper options
        mock_live.place_multi_leg_order.assert_not_called()
        mock_paper.place_multi_leg_order.assert_called_once()

        # 2. Futures entry (Live mode)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            fut_trade = strat._enter_directional_futures_trade(tmp, '261015', snap, 1)

        self.assertFalse(fut_trade['paper'])
        self.assertEqual(fut_trade.get('sub_strategy'), 'DRMA_HM_FUT')
        # Live broker WAS called for live futures
        mock_live.place_market_order.assert_called_once_with('BUY_TO_OPEN', 'ES', 1)
        mock_paper.place_market_order.assert_not_called()

        # 3. Exit live futures trade: closes through live broker
        fut_trade_path = strat._active_trade_path
        strat._exit_trade(tmp, fut_trade_path, reason='profit_target_hit')
        mock_live.place_market_order.assert_called_with('SELL_TO_CLOSE', 'ES', 1)

        # 4. Exit paper options trade: closes without live broker orders
        strat._exit_trade(tmp, dir_trade_path, reason='manual_close')
        # Live broker place_multi_leg_order should not have been called for closing paper trade
        mock_live.place_multi_leg_order.assert_not_called()

    def test_mixed_mode_routing_options_live_futures_paper(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=False,       # Directional options Live
            non_dir_paper=False,   # Non-directional credit Live
            futures_paper=True,    # Futures Paper
            trade_mode='all_three',
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock()
        mock_paper._is_journal = True
        mock_paper._is_paper = True

        mock_live = MagicMock()
        mock_live._is_journal = False
        mock_live._is_paper = False
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.20, order_id='live_opt_order')

        strat._paper_broker = mock_paper
        strat._live_broker = mock_live

        snap = self._make_snap('BEAR', 5800.0, score=4)

        # Non-directional credit spread entry (Live mode)
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5810, 5820, 0.15, 1.20)):
            strat._enter_non_directional_trade(tmp, '261015', snap, 1)

        with open(strat._active_trade_path, encoding='utf-8') as f:
            credit_trade = json.load(f)

        self.assertFalse(credit_trade['paper'])
        self.assertEqual(credit_trade.get('sub_strategy'), 'DRMA_HM_NONDIR')
        mock_live.place_multi_leg_order.assert_called_once()

        # Directional futures entry (Paper mode)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            fut_trade = strat._enter_directional_futures_trade(tmp, '261015', snap, 2)

        self.assertTrue(fut_trade['paper'])
        self.assertEqual(fut_trade.get('sub_strategy'), 'DRMA_HM_FUT')
        # Live market order was NOT called for paper futures
        mock_live.place_market_order.assert_not_called()

    def test_standalone_runner_allows_mixed_mode_without_safety_halt(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            futures_paper=False,  # Mixed mode
        )
        strat = DRMAHMStrategy(cfg)
        runner = StandaloneStrategyRunner(strat, root=tmp)

        # primary broker is live broker (not paper), but config.has_any_live_sub_strategy is True
        strat.broker = MagicMock(_is_journal=False, _is_paper=False)
        # Should not raise RuntimeError because sub-strategies manage their own brokers
        try:
            runner._assert_paper_safety()
        except RuntimeError:
            self.fail('_assert_paper_safety raised RuntimeError unexpectedly for mixed-mode DRMA_HM')

    def test_drma_hm_live_futures_rejection_aborts_entry(self):
        """When live futures order is rejected by broker, strategy must abort and not write active trade."""
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            futures_paper=False,
            trade_mode='directional_futures',
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock()
        mock_live._is_journal = False
        mock_live._is_paper = False
        # Broker rejects the order
        mock_live.place_market_order.return_value = MagicMock(success=False, order_id=None, message='Account not approved for futures')
        strat._live_broker = mock_live

        snap = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            res = strat._enter_directional_futures_trade(tmp, '261015', snap, 1)

        self.assertIsNone(res, 'Entry should return None when broker rejects live order')
        # Check active directory: no open trade file should exist
        active_dir = os.path.join(tmp, 'trades', 'active', 'DRMA_HM_SPX')
        active_trades = [f for f in os.listdir(active_dir) if f.endswith('.json') and not f.startswith('session_')] if os.path.exists(active_dir) else []
        self.assertEqual(len(active_trades), 0, 'No active trade file should be created upon rejected live order')

    def test_drma_hm_futures_profit_target_precision(self):
        """Ensure +0.25 pt gain does NOT trigger +5.0 pt profit target; only full 5.0 pts triggers."""
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            futures_paper=True,
            trade_mode='directional_futures',
            futures_profit_target_pts=5.0,
            futures_stop_loss_pts=5.0,
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock(_is_journal=True, _is_paper=True)
        strat._paper_broker = mock_paper

        snap = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.0):
            strat._enter_directional_futures_trade(tmp, '261015', snap, 1)

        t_path = strat._active_trade_path
        self.assertIsNotNone(t_path)
        with open(t_path, encoding='utf-8') as f:
            trade = json.load(f)
        self.assertEqual(trade['status'], 'open')
        self.assertEqual(trade['entry_price'], 5800.0)
        self.assertEqual(trade['profit_target_price'], 5805.0)

        # Tick 1: Small +0.25 pt uptick to 5800.25 -> MUST NOT EXIT!
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5800.25):
            strat._manage_open_trade(tmp, t_path)
        with open(t_path, encoding='utf-8') as f:
            trade = json.load(f)
        self.assertEqual(trade['status'], 'open', 'Trade must remain open on +0.25 pt tick')
        self.assertEqual(trade['unrealized_pnl'], 0.25)
        self.assertEqual(trade['unrealized_pnl_dollars'], 12.50)

        # Tick 2: Strong +4.75 pt move to 5804.75 -> MUST NOT EXIT!
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5804.75):
            strat._manage_open_trade(tmp, t_path)
        with open(t_path, encoding='utf-8') as f:
            trade = json.load(f)
        self.assertEqual(trade['status'], 'open', 'Trade must remain open on +4.75 pt tick')

        # Tick 3: Hit profit target at 5805.00 -> MUST EXIT with profit_target_hit (+5.0 pts)!
        with patch('strategies.drma_hm.strategy.fetch_es_current_price', return_value=5805.00):
            strat._manage_open_trade(tmp, t_path)

        # Trade should now be closed and moved to history
        hist_dir = os.path.join(tmp, 'trades', 'history', 'DRMA_HM_SPX')
        hist_files = [os.path.join(hist_dir, f) for f in os.listdir(hist_dir) if f.endswith('.json')]
        self.assertEqual(len(hist_files), 1)
        with open(hist_files[0], encoding='utf-8') as f:
            closed_trade = json.load(f)
        self.assertEqual(closed_trade['status'], 'closed')
        self.assertEqual(closed_trade['exit_reason'], 'profit_target_hit (+5.0 pts)')
        self.assertEqual(closed_trade['exit_price'], 5805.0)
        self.assertEqual(closed_trade['realized_pnl'], 5.0)
        self.assertEqual(closed_trade['realized_pnl_dollars'], 250.0)

    def test_drma_hm_directional_debit_order_negative_sign_and_abort(self):
        """Directional options spreads must be placed with negative price (debit) and abort on broker rejection."""
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            dir_paper=False,
            trade_mode='directional_options',
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        # Broker rejects live order
        mock_live.place_multi_leg_order.return_value = MagicMock(success=False, order_id=None, message='Price effect Debit required')
        strat._live_broker = mock_live

        snap = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.50, 3.50)):
            res = strat._enter_directional_trade(tmp, '261015', snap, 1)

        self.assertIsNone(res, 'Entry must return None when live order is rejected')
        mock_live.place_multi_leg_order.assert_called_once()
        _, kwargs = mock_live.place_multi_leg_order.call_args
        self.assertEqual(kwargs['price'], -3.50, 'Price must be negative for opening debit spread')
        self.assertEqual(kwargs['order_type'], 'LIMIT')

        # Verify no open trade file was written to disk
        active_dir = os.path.join(tmp, 'trades', 'active', 'DRMA_HM_SPX')
        active_trades = [f for f in os.listdir(active_dir) if f.endswith('.json') and not f.startswith('session_')] if os.path.exists(active_dir) else []
        self.assertEqual(len(active_trades), 0)

    def test_drma_hm_credit_spread_oco_and_software_breach(self):
        """Credit spread must place OCO on broker, and trigger software breach + OCO cancellation if short breached."""
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            non_dir_paper=False,
            trade_mode='non_directional_options',
            non_dir_profit_target_pct=0.50,
            non_dir_stop_loss_pct=1.00,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, order_id='entry_ord_1', filled_price=1.00)
        mock_live.place_spread_oco_order.return_value = MagicMock(success=True, order_id='oco_ord_999')
        mock_live.get_order_status.return_value = MagicMock(status='working')
        mock_live.cancel_order.return_value = MagicMock(success=True)
        strat._live_broker = mock_live

        snap = self._make_snap('BULL', 5800.0, score=4)
        # Bull Put Credit Spread (PCS): short 5780, long 5770, credit 1.00
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5780, 5770, 0.15, 1.00)):
            strat._enter_non_directional_trade(tmp, '261015', snap, 1)

        # 1. Verify OCO was placed at broker
        mock_live.place_spread_oco_order.assert_called_once()
        _, oco_kwargs = mock_live.place_spread_oco_order.call_args
        self.assertEqual(oco_kwargs['profit_debit'], 0.50)  # 1.00 * (1 - 0.50)
        self.assertEqual(oco_kwargs['stop_trigger_debit'], 2.00)  # 1.00 * (1 + 1.00)

        t_path = strat._active_trade_path
        self.assertIsNotNone(t_path)
        with open(t_path, encoding='utf-8') as f:
            trade = json.load(f)
        self.assertEqual(trade['oco_order_id'], 'oco_ord_999')

        # 2. Spot moves to 5779.0 (below short put 5780) -> Software Breach!
        breach_snap = self._make_snap('BULL', close=5779.0, score=4)
        with patch.object(strat, '_fetch_broker_option_mids', return_value={}):
            strat._manage_open_trade(tmp, t_path, snapshots=[breach_snap])

        # Verify OCO was cancelled
        mock_live.cancel_order.assert_called_with('oco_ord_999')
        # Verify spread close order was submitted
        self.assertEqual(mock_live.place_multi_leg_order.call_count, 2)
        _, close_kwargs = mock_live.place_multi_leg_order.call_args
        self.assertLess(close_kwargs['price'], 0, 'Closing spread must use negative debit limit')

        # Verify trade closed with software_breach reason
        hist_dir = os.path.join(tmp, 'trades', 'history', 'DRMA_HM_SPX')
        hist_files = [os.path.join(hist_dir, f) for f in os.listdir(hist_dir) if f.endswith('.json')]
        self.assertEqual(len(hist_files), 1)
        with open(hist_files[0], encoding='utf-8') as f:
            closed_trade = json.load(f)
        self.assertEqual(closed_trade['status'], 'closed')
        self.assertIn('software_breach', closed_trade['exit_reason'])

    def test_tastytrade_futures_stream_and_mqtt_cache(self):
        """ES futures price lookup queries MQTT price cache and broker without continuous Yahoo polling."""
        from common.mqtt_prices import MqttPriceCache
        cache = MqttPriceCache(topic_prefix='TASTYTRADE/')
        # Simulate MQTT message received for /ESZ26:XCME
        cache._prices['/ESZ26:XCME'] = 5825.25
        cache._prices['ES'] = 5825.25
        cache._last_msg_at = time.time()

        self.assertEqual(cache.get('ES'), 5825.25)
        self.assertEqual(cache.get('/ES'), 5825.25)
        self.assertEqual(cache.get_price('ES'), 5825.25)

        # Verify fetch_es_current_price uses MQTT cache without calling Yahoo
        with patch('common.mqtt_prices.get_mqtt_price_cache', return_value=cache):
            price = fetch_es_current_price(broker=None)
            self.assertEqual(price, 5825.25)



class TestDRMAHMSameSideLock(unittest.TestCase):
    @staticmethod
    def _make_snap(bias='BULL', close=5800.0, score=5):
        grade = 'GRADE A+ (5/5)' if score == 5 else ('GRADE A (4/5)' if score == 4 else 'GRADE B (3/5)')
        return IndicatorSnapshot(
            bar_index=15, datetime_str='2026-10-15 09:30:00',
            close=close, open=5795.0, high=5802.0, low=5794.0,
            out_ma=5790.0, ma_delta_2=1.0, ma_slope_up=True, ma_slope_down=False,
            is_not_extended=True, orb_high=5810.0, orb_low=5780.0, orb_mid=5795.0,
            orb_formed=True, pdh=5820.0, pdl=5770.0, pdc=5795.0, onh=5815.0, onl=5785.0,
            rsi=60.0, hm_fast=58.0, hm_slow=54.0,
            is_super_bull=(bias == 'BULL'), is_super_bear=(bias == 'BEAR'),
            is_bull_pullback=False, is_bear_bounce=False,
            bull_score=score if bias == 'BULL' else 0,
            bear_score=0 if bias == 'BULL' else score,
            active_score=score, bias=bias,
            setup_grade=grade,
            trade_vehicle='BUY CALLS / CDS' if score == 5 else 'SELL PCS',
            gamma_risk='NORMAL / THETA SAFE',
        )

    def test_dir_blocked_when_same_side_nondir_active_both_paper(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=True,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        # Active credit spread: PCS on BULL side, paper=True
        strat._credit_options_position_state = 1
        strat._credit_options_trade_paper = True

        snap_bull = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # Directional options remains FLAT because blocked
        self.assertEqual(strat._dir_options_position_state, 0)

    def test_dir_blocked_when_same_side_nondir_active_both_live(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            dir_paper=False,
            non_dir_paper=False,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='dir_1')
        strat._live_broker = mock_live

        # Active credit spread: CCS on BEAR side, paper=False
        strat._credit_options_position_state = -1
        strat._credit_options_trade_paper = False

        snap_bear = self._make_snap('BEAR', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5790, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bear, 1)

        # Directional options remains FLAT because blocked
        self.assertEqual(strat._dir_options_position_state, 0)
        mock_live.place_multi_leg_order.assert_not_called()

    def test_dir_enters_when_same_side_nondir_active_mixed_nondir_live_dir_paper(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,       # Directional is PAPER
            non_dir_paper=False,  # Non-directional is LIVE
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock(_is_journal=True, _is_paper=True)
        mock_paper.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='paper_dir_1')
        strat._paper_broker = mock_paper
        strat.broker = mock_paper

        # Active credit spread: PCS on BULL side, entered LIVE
        strat._credit_options_position_state = 1
        strat._credit_options_trade_paper = False

        snap_bull = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # In mixed modes, setting does NOT kick in -> directional enters successfully!
        self.assertEqual(strat._dir_options_position_state, 1)

    def test_dir_enters_when_same_side_nondir_active_mixed_nondir_paper_dir_live(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=False,      # Directional is LIVE
            non_dir_paper=True,   # Non-directional is PAPER
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='live_dir_1')
        strat._live_broker = mock_live
        strat.broker = mock_live

        # Active credit spread: CCS on BEAR side, entered PAPER
        strat._credit_options_position_state = -1
        strat._credit_options_trade_paper = True

        snap_bear = self._make_snap('BEAR', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5790, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bear, 1)

        # In mixed modes, setting does NOT kick in -> directional enters successfully!
        self.assertEqual(strat._dir_options_position_state, -1)
        mock_live.place_multi_leg_order.assert_called_once()

    def test_dir_enters_when_opposite_side_nondir_active(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=True,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock(_is_journal=True, _is_paper=True)
        mock_paper.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='paper_dir_2')
        strat._paper_broker = mock_paper
        strat.broker = mock_paper

        # Active credit spread: PCS on BULL side (+1)
        strat._credit_options_position_state = 1
        strat._credit_options_trade_paper = True

        # Incoming setup: BEAR (-1)
        snap_bear = self._make_snap('BEAR', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5790, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bear, 1)

        # Opposite side is not blocked -> directional enters!
        self.assertEqual(strat._dir_options_position_state, -1)

    def test_dir_enters_when_lock_disabled(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=True,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=False,
        )
        strat = DRMAHMStrategy(cfg)
        mock_paper = MagicMock(_is_journal=True, _is_paper=True)
        mock_paper.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='paper_dir_3')
        strat._paper_broker = mock_paper
        strat.broker = mock_paper

        # Active credit spread on BULL side (+1), both PAPER
        strat._credit_options_position_state = 1
        strat._credit_options_trade_paper = True

        snap_bull = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # Lock disabled -> directional enters!
        self.assertEqual(strat._dir_options_position_state, 1)

    def test_dir_iron_condor_lock_modes(self):
        tmp = tempfile.mkdtemp()
        # Case A: IC is active (state 2) and both are PAPER -> blocked
        cfg_paper = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=True,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat_a = DRMAHMStrategy(cfg_paper)
        strat_a._credit_options_position_state = 2  # Iron Condor
        strat_a._credit_options_trade_paper = True

        snap_bull = self._make_snap('BULL', 5800.0, score=5)
        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat_a._evaluate_and_enter(tmp, '261015', snap_bull, 1)
        self.assertEqual(strat_a._dir_options_position_state, 0)

        # Case B: IC is LIVE (state 2), but directional is PAPER -> ENTERS!
        cfg_mixed = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=False,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat_b = DRMAHMStrategy(cfg_mixed)
        mock_p = MagicMock(_is_journal=True, _is_paper=True)
        mock_p.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=2.50, order_id='p_dir')
        strat_b._paper_broker = mock_p
        strat_b.broker = mock_p
        strat_b._credit_options_position_state = 2  # Iron Condor (LIVE)
        strat_b._credit_options_trade_paper = False

        with patch('strategies.drma_hm.strategy.select_directional_strikes', return_value=(5800, 5810, 0.40, 2.50)):
            strat_b._evaluate_and_enter(tmp, '261015', snap_bull, 1)
        self.assertEqual(strat_b._dir_options_position_state, 1)

    def test_nondir_blocked_when_same_side_dir_active_both_live(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            dir_paper=False,
            non_dir_paper=False,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.00, order_id='nd_1')
        strat._live_broker = mock_live

        # Active directional spread: Bull Call Debit Spread (state 1, live)
        strat._dir_options_position_state = 1
        strat._dir_options_trade_paper = False

        snap_bull = self._make_snap('BULL', 5800.0, score=4)  # Score 4 -> Non-Directional
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5750, 5740, 0.15, 1.00)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # Non-directional options remains FLAT because blocked
        self.assertEqual(strat._credit_options_position_state, 0)
        mock_live.place_multi_leg_order.assert_not_called()

    def test_nondir_blocked_when_same_side_dir_active_both_paper(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,
            non_dir_paper=True,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        # Active directional spread: Bear Put Debit Spread (state -1, paper)
        strat._dir_options_position_state = -1
        strat._dir_options_trade_paper = True

        snap_bear = self._make_snap('BEAR', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5850, 5860, 0.15, 1.00)):
            strat._evaluate_and_enter(tmp, '261015', snap_bear, 1)

        self.assertEqual(strat._credit_options_position_state, 0)

    def test_nondir_enters_when_same_side_dir_active_mixed_modes(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=True,
            dir_paper=True,       # Directional is PAPER
            non_dir_paper=False,  # Non-directional is LIVE
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.00, order_id='nd_live_1')
        strat._live_broker = mock_live

        # Active directional spread: Bull Call Debit Spread (state 1, paper)
        strat._dir_options_position_state = 1
        strat._dir_options_trade_paper = True

        snap_bull = self._make_snap('BULL', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5750, 5740, 0.15, 1.00)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # In mixed modes, setting does NOT kick in -> non-directional enters successfully!
        self.assertEqual(strat._credit_options_position_state, 1)
        mock_live.place_multi_leg_order.assert_called_once()

    def test_nondir_enters_when_opposite_side_dir_active(self):
        tmp = tempfile.mkdtemp()
        cfg = DRMAHMConfig(
            root=tmp,
            paper=False,
            dir_paper=False,
            non_dir_paper=False,
            trade_mode='all_three',
            dir_block_if_same_side_nondir=True,
        )
        strat = DRMAHMStrategy(cfg)
        mock_live = MagicMock(_is_journal=False, _is_paper=False)
        mock_live.place_multi_leg_order.return_value = MagicMock(success=True, filled_price=1.00, order_id='nd_live_opp')
        strat._live_broker = mock_live

        # Active directional spread: Bear Put Debit Spread (state -1, LIVE)
        strat._dir_options_position_state = -1
        strat._dir_options_trade_paper = False

        # Incoming signal is BULL (opposite side!)
        snap_bull = self._make_snap('BULL', 5800.0, score=4)
        with patch('strategies.drma_hm.strategy.select_non_directional_strikes', return_value=(5750, 5740, 0.15, 1.00)):
            strat._evaluate_and_enter(tmp, '261015', snap_bull, 1)

        # Opposite side -> permitted!
        self.assertEqual(strat._credit_options_position_state, 1)
        mock_live.place_multi_leg_order.assert_called_once()


if __name__ == '__main__':
    unittest.main()


