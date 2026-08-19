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
        self.assertFalse(cfg.paper)
        self.assertTrue(cfg.confirmed)
        self.assertFalse(cfg.scheduled)
        self.assertEqual(cfg.entry_time_et, '08:45')
        self.assertEqual(cfg.spread_width, 5)
        self.assertEqual(cfg.target_credit, 1.0)
        self.assertEqual(strat.schedule(), [])


if __name__ == '__main__':
    unittest.main()
