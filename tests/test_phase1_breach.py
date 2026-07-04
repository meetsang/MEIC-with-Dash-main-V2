"""Unit tests for Phase 1 spread breach comparison."""
import unittest

from blocks.stop.breach import spread_breach_triggered, spread_mark_price


class TestPhase1Breach(unittest.TestCase):
    def test_spread_mark_price(self):
        self.assertEqual(spread_mark_price(4.0, 1.5), 2.5)

    def test_no_breach_below_threshold(self):
        # short 4, long 2 → spread 2; 2x stop on ~3.9 fill ≈ 7.7
        spread = spread_mark_price(4.0, 2.0)
        self.assertFalse(spread_breach_triggered(spread, 7.7))

    def test_breach_at_threshold(self):
        spread = spread_mark_price(8.0, 0.5)
        self.assertTrue(spread_breach_triggered(spread, 7.5))

    def test_breach_when_short_rallies(self):
        # Market moves against CCS: short up more than long
        spread = spread_mark_price(10.0, 2.0)
        stop = 7.8  # ~2x (4-0.1)*2
        self.assertTrue(spread_breach_triggered(spread, stop))

    def test_no_breach_cheaper_long_offsets(self):
        spread = spread_mark_price(8.0, 4.0)
        self.assertFalse(spread_breach_triggered(spread, 7.8))


if __name__ == '__main__':
    unittest.main()
