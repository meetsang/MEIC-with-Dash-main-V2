"""G9 — CreditEntryConfig validation."""
from __future__ import annotations

import unittest

from blocks.entry.config import CreditEntryConfig


class TestCreditEntryConfig(unittest.TestCase):
    def test_defaults_valid(self):
        cfg = CreditEntryConfig.from_meic_config()
        self.assertGreaterEqual(cfg.credit_max_put, cfg.credit_min)

    def test_credit_max_below_min_raises(self):
        with self.assertRaises(ValueError):
            CreditEntryConfig(credit_min=2.0, credit_max_put=1.0, credit_max_call=1.5)

    def test_spread_width_inverted_raises(self):
        with self.assertRaises(ValueError):
            CreditEntryConfig(spread_width_min=40, spread_width_max=25)

    def test_otm_inverted_raises(self):
        with self.assertRaises(ValueError):
            CreditEntryConfig(otm_min=100, otm_max=50)

    def test_invalid_quote_source_raises(self):
        with self.assertRaises(ValueError):
            CreditEntryConfig(quote_source='rest')

    def test_from_overrides_validates(self):
        cfg = CreditEntryConfig.from_overrides({'quantity': 2})
        self.assertEqual(cfg.quantity, 2)

    def test_from_overrides_unknown_key_raises(self):
        with self.assertRaises(ValueError):
            CreditEntryConfig.from_overrides({'bogus': 1})


if __name__ == '__main__':
    unittest.main()
