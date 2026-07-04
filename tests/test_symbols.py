"""Unit tests for option symbol translation."""
import unittest

from common.symbols import (
    build_schwab_symbol,
    build_tastytrade_symbol,
    parse_canonical,
    strike_from_symbol,
    symbols_equivalent,
    to_schwab,
    to_tastytrade,
)


class TestSymbols(unittest.TestCase):
    def test_schwab_to_tastytrade(self):
        schwab = 'SPXW  260619P05550000'
        tt = to_tastytrade(schwab)
        self.assertEqual(tt, '.SPXW260619P5550')

    def test_tastytrade_to_schwab(self):
        tt = '.SPXW260619C5600'
        schwab = to_schwab(tt)
        self.assertTrue(schwab.startswith('SPXW'))
        self.assertIn('260619C', schwab)
        self.assertEqual(strike_from_symbol(schwab), 5600)

    def test_build_symbols(self):
        self.assertEqual(
            build_tastytrade_symbol('260619', 'P', 5550),
            '.SPXW260619P5550',
        )
        self.assertIn('05550000', build_schwab_symbol('260619', 'P', 5550))

    def test_parse_roundtrip(self):
        for sym in ('.SPXW260619P5550', 'SPXW  260619P05550000'):
            parsed = parse_canonical(sym)
            self.assertIsNotNone(parsed)
            expiry, opt_type, strike = parsed
            self.assertEqual(expiry, '260619')
            self.assertEqual(opt_type, 'P')
            self.assertEqual(strike, 5550)

    def test_symbols_equivalent(self):
        tt = '.SPXW260622C7635'
        occ = 'SPXW  260622C07635000'
        self.assertTrue(symbols_equivalent(tt, occ))


if __name__ == '__main__':
    unittest.main()
