"""Shim import path for spread_scan relocation."""
from __future__ import annotations

import importlib
import unittest
import warnings


class TestSpreadScanShim(unittest.TestCase):
    def test_deprecated_shim_reexports(self):
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)
            mod = importlib.import_module('meic0dte.open.spread_scan')
        self.assertIs(mod.scan_credit_spreads, importlib.import_module('blocks.entry.spread_scan').scan_credit_spreads)


if __name__ == '__main__':
    unittest.main()
