"""Tests for 0DTE SPX Mean Reversion Strategy."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from common.standalone_runner import StandaloneStrategyRunner
from strategies.mean_reversion.legs import (
    build_closing_legs,
    build_spread_legs,
    compute_expected_move,
    compute_expected_move_boundaries,
    estimate_0dte_delta,
    round_atm,
    select_credit_spread_strikes,
)
from strategies.mean_reversion.settlement import pnl_points_to_dollars, update_unrealized_marks
from strategies.mean_reversion.strategy import MeanReversionConfig, MeanReversionStrategy


class TestMeanReversionLegs(unittest.TestCase):
    def test_round_atm(self):
        self.assertEqual(round_atm(5802.4, 5), 5800)
        self.assertEqual(round_atm(5802.6, 5), 5805)
        self.assertEqual(round_atm(5800.0, 5), 5800)

    def test_expected_move_calculation(self):
        call_mid = 11.20
        put_mid = 9.80
        straddle_price = round(call_mid + put_mid, 2)
        self.assertEqual(straddle_price, 21.00)

        em = compute_expected_move(straddle_price, multiplier=1.0)
        self.assertEqual(em, 21.00)

        open_spx = 5800.0
        upper_em, lower_em = compute_expected_move_boundaries(open_spx, em)
        self.assertEqual(upper_em, 5821.00)
        self.assertEqual(lower_em, 5779.00)

    def test_delta_estimation(self):
        spot = 5800.0
        # ATM call roughly ~0.50
        atm_call_delta = estimate_0dte_delta(spot, 5800, 'C', iv=0.18, dte_hours=6.5)
        self.assertAlmostEqual(atm_call_delta, 0.50, delta=0.08)

        # OTM call should be lower
        otm_call_delta = estimate_0dte_delta(spot, 5825, 'C', iv=0.18, dte_hours=6.5)
        self.assertTrue(0.05 <= otm_call_delta <= 0.35)

        # ATM put roughly ~ -0.50
        atm_put_delta = estimate_0dte_delta(spot, 5800, 'P', iv=0.18, dte_hours=6.5)
        self.assertAlmostEqual(atm_put_delta, -0.50, delta=0.08)

        # OTM put should be between -0.05 and -0.35
        otm_put_delta = estimate_0dte_delta(spot, 5775, 'P', iv=0.18, dte_hours=6.5)
        self.assertTrue(-0.35 <= otm_put_delta <= -0.05)

    def test_strike_selection_call_spread(self):
        # Upper EM breach at 5825 -> Bear Call Spread
        spot = 5825.0
        short_k, long_k, delta = select_credit_spread_strikes(
            side='C',
            spot=spot,
            spread_width=10,
            target_delta=0.18,
            min_delta=0.10,
            max_delta=0.25,
            expiry_yymmdd='261002',
        )
        self.assertGreater(short_k, spot)  # Must be OTM
        self.assertEqual(long_k, short_k + 10)  # 10-wide
        self.assertTrue(0.10 <= delta <= 0.25)

    def test_strike_selection_put_spread(self):
        # Lower EM breach at 5775 -> Bull Put Spread
        spot = 5775.0
        short_k, long_k, delta = select_credit_spread_strikes(
            side='P',
            spot=spot,
            spread_width=10,
            target_delta=0.18,
            min_delta=0.10,
            max_delta=0.25,
            expiry_yymmdd='261002',
        )
        self.assertLess(short_k, spot)  # Must be OTM
        self.assertEqual(long_k, short_k - 10)  # 10-wide
        self.assertTrue(-0.25 <= delta <= -0.10)

    def test_build_legs(self):
        legs = build_spread_legs(5840, 5850, 'C', '261002', quantity=2)
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].action, 'SELL_TO_OPEN')
        self.assertEqual(legs[0].quantity, 2)
        self.assertIn('C5840', legs[0].symbol)
        self.assertEqual(legs[1].action, 'BUY_TO_OPEN')
        self.assertEqual(legs[1].quantity, 2)
        self.assertIn('C5850', legs[1].symbol)

        close_legs = build_closing_legs(5840, 5850, 'C', '261002', quantity=2)
        self.assertEqual(close_legs[0].action, 'BUY_TO_CLOSE')
        self.assertEqual(close_legs[1].action, 'SELL_TO_CLOSE')


class TestMeanReversionRiskManagement(unittest.TestCase):
    def test_pnl_conversions(self):
        self.assertEqual(pnl_points_to_dollars(1.50, quantity=1), 150.00)
        self.assertEqual(pnl_points_to_dollars(-2.00, quantity=2), -400.00)

    def test_update_unrealized_marks(self):
        trade = {
            'entry_credit': 2.00,
            'quantity': 1,
            'structures': [{
                'net_credit': 2.00,
                'legs': [
                    {'symbol': '.SPXW261002C5840', 'action': 'SELL_TO_OPEN', 'qty': 1},
                    {'symbol': '.SPXW261002C5850', 'action': 'BUY_TO_OPEN', 'qty': 1},
                ],
            }],
        }
        mids = {
            '.SPXW261002C5840': 1.20,
            '.SPXW261002C5850': 0.20,
        }
        # Mark = 1.20 - 0.20 = 1.00
        # PnL = 2.00 - 1.00 = +1.00 pt = +$100
        pnl = update_unrealized_marks(trade, mids)
        self.assertEqual(pnl, 1.00)
        self.assertEqual(trade['unrealized_pnl'], 1.00)
        self.assertEqual(trade['unrealized_pnl_dollars'], 100.00)

    def test_exit_thresholds_logic(self):
        entry_credit = 2.00
        profit_target_pct = 0.50
        stop_loss_pct = 1.00
        be_trigger_pct = 0.25
        be_lock_pct = 0.10

        target_price = round(entry_credit * (1.0 - profit_target_pct), 2)
        initial_stop = round(entry_credit * (1.0 + stop_loss_pct), 2)
        be_stop = round(entry_credit * (1.0 - be_lock_pct), 2)

        self.assertEqual(target_price, 1.00)  # 50% profit target
        self.assertEqual(initial_stop, 4.00)  # 100% loss initial stop
        self.assertEqual(be_stop, 1.80)      # Lock in 10% on breakeven

        # At mark 1.50 -> 25% profit reached -> BE triggered
        mark = 1.50
        profit_pct = (entry_credit - mark) / entry_credit
        self.assertGreaterEqual(profit_pct, be_trigger_pct)

        # At mark 1.00 -> 50% profit target reached
        mark = 1.00
        self.assertLessEqual(mark, target_price)


class TestMeanReversionStrategyHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = MeanReversionConfig(
            name='MEAN_REVERSION_SPX',
            enabled=True,
            confirmed=True,
            paper=True,
            root=self.tmp,
        )
        self.strategy = MeanReversionStrategy(self.cfg)

    def test_skips_reentry_when_session_entered(self):
        active = os.path.join(self.tmp, 'trades', 'active', 'MEAN_REVERSION_SPX')
        os.makedirs(active, exist_ok=True)
        today = date.today().isoformat()
        with open(os.path.join(active, f'session_{today}.json'), 'w', encoding='utf-8') as f:
            json.dump({'entered': True, 'skip_reason': None, 'overrides': {}}, f)

        runner = StandaloneStrategyRunner(self.strategy, root=self.tmp)
        with patch.object(runner, '_register_open_trade_streamer_legs') as reg:
            runner.start()
            reg.assert_called_once()
        self.assertFalse(runner._running)

    def test_session_state_saving(self):
        runner = StandaloneStrategyRunner(self.strategy, root=self.tmp)
        runner.save_session_state(entered=True, upper_em=5820.0, lower_em=5780.0)
        state = runner._load_session_state()
        self.assertTrue(state.get('entered'))
        self.assertEqual(state.get('upper_em'), 5820.0)

    def test_record_skipped_attempt_on_thin_credit(self):
        self.strategy._record_skipped_attempt(
            root=self.tmp,
            side='C',
            signal='UPPER_EM_BREACH',
            spot=5821.50,
            em_level=5821.00,
            short_k=5835,
            long_k=5845,
            delta=0.18,
            credit=0.15,
            min_credit=0.50,
            reason='Net credit ($0.15) < min threshold ($0.50)',
        )
        state = self.strategy._load_session_state(self.tmp)
        self.assertFalse(state.get('entered'))
        self.assertEqual(state.get('skip_reason'), 'thin_credit')
        skipped = state.get('skipped_attempts', [])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]['credit'], 0.15)
        self.assertEqual(skipped[0]['min_credit'], 0.50)
        self.assertEqual(skipped[0]['strikes'], '5835/5845')


if __name__ == '__main__':
    unittest.main()
