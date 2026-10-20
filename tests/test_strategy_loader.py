"""Load strategies.yaml registry."""
from __future__ import annotations

import unittest

from strategies.loader import load_enabled_strategies, load_strategies


class TestStrategyLoader(unittest.TestCase):
    def test_loads_meic_and_manual(self):
        strategies = load_enabled_strategies()
        names = {s.config.name for s in strategies}
        self.assertIn('MEIC_IC', names)
        self.assertIn('MANUAL_SPREAD', names)

    def test_meic_is_scheduled_manual_is_not(self):
        by_name = {s.config.name: s for s in load_enabled_strategies()}
        self.assertTrue(by_name['MEIC_IC'].schedule())
        self.assertEqual(by_name['MANUAL_SPREAD'].schedule(), [])

    def test_skyline_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('SKYLINE_SPX', standalone)
        strat, cfg = standalone['SKYLINE_SPX']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(strat.schedule(), [])

    def test_perma_bull_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('PERMA_BULL_PCS', standalone)
        strat, cfg = standalone['PERMA_BULL_PCS']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.entry_time_et, '08:45')
        self.assertEqual(cfg.spread_width, 5)
        self.assertEqual(cfg.target_credit, 1.0)
        self.assertEqual(strat.schedule(), [])

    def test_mean_reversion_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('MEAN_REVERSION_SPX', standalone)
        strat, cfg = standalone['MEAN_REVERSION_SPX']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.open_time_cst, '08:30')
        self.assertEqual(cfg.cutoff_time_cst, '12:00')
        self.assertEqual(cfg.spread_width, 10)
        self.assertEqual(cfg.profit_target_pct, 0.50)
        self.assertEqual(cfg.stop_loss_pct, 1.00)
        self.assertEqual(strat.schedule(), [])

    def test_iron_fly_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('IRON_FLY_SPX', standalone)
        strat, cfg = standalone['IRON_FLY_SPX']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.mode, 'PAPER')
        self.assertEqual(cfg.contracts, 1)
        self.assertEqual(cfg.max_open_risk, 15000.0)
        self.assertEqual(cfg.daily_loss_stop, 2500.0)
        self.assertEqual(strat.schedule(), [])

    def test_iron_fly_xsp_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('IRON_FLY_XSP', standalone)
        strat, cfg = standalone['IRON_FLY_XSP']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.mode, 'PAPER')
        self.assertEqual(cfg.contracts, 2)
        self.assertEqual(cfg.max_open_risk, 3000.0)
        self.assertEqual(cfg.daily_loss_stop, 500.0)
        self.assertEqual(strat.schedule(), [])

    def test_drma_hm_loaded_when_enabled(self):
        from strategies.loader import load_standalone_strategies

        standalone = {cfg.name: (strat, cfg) for strat, cfg in load_standalone_strategies()}
        self.assertIn('DRMA_HM_SPX', standalone)
        strat, cfg = standalone['DRMA_HM_SPX']
        self.assertTrue(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.ticker, 'SPX')
        self.assertEqual(cfg.trade_mode, 'all_three')
        self.assertTrue(cfg.is_directional_futures_enabled)
        self.assertEqual(cfg.futures_symbol, 'ES')
        self.assertEqual(cfg.futures_min_grade, 'Grade A+ Only')
        self.assertEqual(cfg.futures_profit_target_pts, 5.0)
        self.assertEqual(cfg.futures_stop_loss_pts, 5.0)
        self.assertEqual(cfg.ma_type, 'EMA')
        self.assertEqual(cfg.ma_len, 21)
        self.assertEqual(cfg.non_dir_spread_width, 10)
        self.assertEqual(cfg.dir_spread_width, 10)
        self.assertEqual(strat.schedule(), [])


if __name__ == '__main__':
    unittest.main()

