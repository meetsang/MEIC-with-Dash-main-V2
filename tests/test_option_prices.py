"""Tests for option mid sanitization."""
from __future__ import annotations

import unittest

from common.option_prices import is_option_symbol, sanitize_option_mid


class TestOptionPrices(unittest.TestCase):
    def test_is_option_symbol(self):
        self.assertTrue(is_option_symbol('.SPXW260701C7530'))
        self.assertFalse(is_option_symbol('SPX'))

    def test_rejects_index_noise(self):
        self.assertIsNone(sanitize_option_mid('.SPXW260701C7530', 7505.0))
        self.assertEqual(sanitize_option_mid('.SPXW260701C7530', 1.25), 1.25)

    def test_spx_not_capped(self):
        self.assertEqual(sanitize_option_mid('SPX', 7505.0), 7505.0)


if __name__ == '__main__':
    unittest.main()
