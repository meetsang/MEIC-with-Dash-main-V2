"""Tests for generalized trades layout."""
from __future__ import annotations

import unittest

from common import trades_layout


class TestTradesLayout(unittest.TestCase):
    def test_meic_path_unchanged(self):
        path = trades_layout.active_dir_for_strategy('MEIC_IC')
        self.assertTrue(path.endswith('trades/active/MEIC_IC'.replace('/', '\\')) or
                        path.endswith('trades/active/MEIC_IC'))

    def test_manual_path_unchanged(self):
        path = trades_layout.active_dir_for_strategy('MANUAL_SPREAD')
        self.assertTrue(path.endswith('trades/active/MANUAL_SPREAD'.replace('/', '\\')) or
                        path.endswith('trades/active/MANUAL_SPREAD'))

    def test_skyline_path(self):
        path = trades_layout.active_dir_for_strategy('SKYLINE_SPX')
        self.assertIn('SKYLINE_SPX', path)


if __name__ == '__main__':
    unittest.main()
