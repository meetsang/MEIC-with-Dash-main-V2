"""Tests for Skyline butterfly leg builder."""
from __future__ import annotations

import unittest

from strategies.skyline.legs import ButterflyConfig, build_butterfly_legs, round_atm


class TestSkylineLegs(unittest.TestCase):
    def test_round_atm(self):
        self.assertEqual(round_atm(6402, 5), 6400)
        self.assertEqual(round_atm(6403, 5), 6405)

    def test_bf1_default_strikes(self):
        bf = ButterflyConfig('BF1', 'call', 0, 25, 50)
        legs = build_butterfly_legs(6400, '260728', bf, quantity=1)
        self.assertEqual(len(legs), 3)
        self.assertEqual(legs[0].action, 'BUY_TO_OPEN')
        self.assertEqual(legs[1].action, 'SELL_TO_OPEN')
        self.assertEqual(legs[1].quantity, 2)
        self.assertIn('C6400', legs[0].symbol)
        self.assertIn('C6425', legs[1].symbol)
        self.assertIn('C6450', legs[2].symbol)

    def test_bf4_put_strikes(self):
        bf = ButterflyConfig('BF4', 'put', -35, -60, -80)
        legs = build_butterfly_legs(6400, '260728', bf)
        self.assertIn('P6365', legs[0].symbol)
        self.assertIn('P6340', legs[1].symbol)
        self.assertIn('P6320', legs[2].symbol)


if __name__ == '__main__':
    unittest.main()
