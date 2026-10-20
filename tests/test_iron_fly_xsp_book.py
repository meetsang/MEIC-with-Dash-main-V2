"""Unit tests for XSP Iron Fly book and paired fly state."""
from __future__ import annotations

import tempfile
import unittest

from strategies.iron_fly.book import FlyStatus, IronFly
from strategies.iron_fly_xsp.book import IronFlyXspBook, IronFlyXspPair


class TestIronFlyXspBook(unittest.TestCase):
    def test_paired_fly_lifecycle(self):
        fly_a = IronFly(
            id='xsp-1a',
            center=775,
            cw=3,
            pw=3,
            call_long=778,
            put_long=772,
            credit=2.30,
            quantity=1,
            open_risk=70.0,
            fees_paid=5.20,
        )
        fly_b = IronFly(
            id='xsp-1b',
            center=776,
            cw=3,
            pw=3,
            call_long=779,
            put_long=773,
            credit=2.40,
            quantity=1,
            open_risk=60.0,
            fees_paid=5.20,
        )
        pair = IronFlyXspPair(
            pair_id='xsp-pair-1',
            spx_center=7755,
            center_ideal=775.5,
            fly_a=fly_a,
            fly_b=fly_b,
            status=FlyStatus.OPEN,
        )

        self.assertEqual(pair.credit_total, 4.70)
        self.assertEqual(pair.credit_avg, 2.35)
        # BE Low = 775.5 - 2.35 = 773.15
        self.assertEqual(pair.be_low, 773.15)
        # BE High = 775.5 + 2.35 = 777.85
        self.assertEqual(pair.be_high, 777.85)
        self.assertEqual(pair.open_risk, 130.0)
        self.assertEqual(pair.fees_paid, 10.40)

    def test_book_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = IronFlyXspBook('2026-10-03', root=tmp)
            fly_a = IronFly(id='xsp-1a', center=775, cw=3, pw=3, call_long=778, put_long=772, credit=2.30)
            fly_b = IronFly(id='xsp-1b', center=776, cw=3, pw=3, call_long=779, put_long=773, credit=2.40)
            pair = IronFlyXspPair(
                pair_id='xsp-pair-1',
                spx_center=7755,
                center_ideal=775.5,
                fly_a=fly_a,
                fly_b=fly_b,
                status=FlyStatus.OPEN,
            )
            book.add_pair(pair)
            book.em_open = 4.80
            book.em_now = 3.90
            book.ratio_vs_83 = 0.8125
            book.save()

            loaded = IronFlyXspBook.load_or_create('2026-10-03', root=tmp)
            self.assertEqual(len(loaded.pairs), 1)
            self.assertEqual(len(loaded.flies), 2)
            self.assertEqual(loaded.em_open, 4.80)
            self.assertEqual(loaded.ratio_vs_83, 0.8125)
            self.assertIn('xsp-pair-1', loaded.pairs)
            self.assertIn('xsp-1a', loaded.flies)
            self.assertIn('xsp-1b', loaded.flies)

            # Check structures list compatibility for manual_close.py
            d = loaded.to_dict()
            self.assertEqual(len(d['structures']), 2)
