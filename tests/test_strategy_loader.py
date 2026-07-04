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


if __name__ == '__main__':
    unittest.main()
