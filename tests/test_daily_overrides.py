"""Tests for daily overrides."""
from __future__ import annotations

import os
import tempfile
import unittest

from common.daily_overrides import delete_override, effective_config, get_overrides, set_overrides
from strategies.skyline.strategy import SkylineConfig


class TestDailyOverrides(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_set_and_get_override(self):
        cfg = SkylineConfig()
        set_overrides('SKYLINE_SPX', self.tmp, {'gap_down_skip_points': 30}, SkylineConfig)
        overrides = get_overrides('SKYLINE_SPX', self.tmp)
        self.assertEqual(overrides['gap_down_skip_points'], 30)
        eff = effective_config(cfg, 'SKYLINE_SPX', self.tmp)
        self.assertEqual(eff.gap_down_skip_points, 30)

    def test_delete_override(self):
        set_overrides('SKYLINE_SPX', self.tmp, {'quantity': 2}, SkylineConfig)
        delete_override('SKYLINE_SPX', self.tmp, 'quantity')
        self.assertNotIn('quantity', get_overrides('SKYLINE_SPX', self.tmp))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            set_overrides('SKYLINE_SPX', self.tmp, {'not_a_field': 1}, SkylineConfig)


if __name__ == '__main__':
    unittest.main()
