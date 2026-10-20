"""Unit tests for Iron Fly Book and state tracking."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from strategies.iron_fly.book import IronFly, IronFlyBook


class TestIronFlyBook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_book_lifecycle_and_calculations(self):
        book = IronFlyBook(day='2026-10-02', mode='PAPER')
        self.assertEqual(len(book.get_open_flies()), 0)
        self.assertEqual(book.open_risk(), 0.0)

        # Add an open fly
        fly1 = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=25,
            put_width=20,
            qty=1,
            status='OPEN',
            planned_credit=18.0,
            fill_credit=18.0,
            be_low=5782.0,
            be_high=5818.0,
            max_loss=700.0,  # (25 - 18) * 100
            fees=5.20,
        )
        book.flies[fly1.id] = fly1

        self.assertEqual(len(book.get_open_flies()), 1)
        self.assertEqual(book.get_open_centers(), [5800.0])
        self.assertEqual(book.open_risk(), 700.0)
        self.assertEqual(book.total_fees(), 5.20)

        # Check clash maps (§2.6a)
        short_map = book.get_short_legs_map()
        self.assertEqual(short_map.get(('C', 5800.0)), 1)
        self.assertEqual(short_map.get(('P', 5800.0)), 1)

        long_map = book.get_long_legs_map()
        self.assertEqual(long_map.get(('C', 5825.0)), 1)  # 5800 + 25
        self.assertEqual(long_map.get(('P', 5780.0)), 1)  # 5800 - 20

        # Close the fly with cut
        fly1.status = 'CLOSED'
        fly1.exit_debit = 19.50
        fly1.realized_pnl = (18.0 - 19.50) * 100  # -150.0
        fly1.fees += 5.20  # cut fee

        self.assertEqual(len(book.get_open_flies()), 0)
        self.assertEqual(book.open_risk(), 0.0)
        self.assertEqual(book.realized_pnl(), -150.0)
        self.assertEqual(book.total_fees(), 10.40)
        self.assertEqual(book.net_pnl(), -160.40)

    def test_book_json_save_load(self):
        book = IronFlyBook(day='2026-10-02', mode='PAPER', em_open=28.50, current_tier=2)
        fly = IronFly(
            id='IF_20261002_1',
            seq=1,
            day='2026-10-02',
            center=5800.0,
            call_width=20,
            put_width=20,
            qty=1,
            status='OPEN',
            planned_credit=17.50,
            fill_credit=17.50,
            be_low=5782.5,
            be_high=5817.5,
            max_loss=250.0,
            fees=5.20,
        )
        book.flies[fly.id] = fly
        book.add_event('TEST', 'Test message')

        path = os.path.join(self.tmp, 'iron_fly.json')
        book.save_to_json(path)

        loaded = IronFlyBook.load_from_json(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.day, '2026-10-02')
        self.assertEqual(loaded.em_open, 28.50)
        self.assertEqual(loaded.current_tier, 2)
        self.assertIn('IF_20261002_1', loaded.flies)
        self.assertEqual(loaded.flies['IF_20261002_1'].center, 5800.0)
        self.assertEqual(len(loaded.events), 1)

    def test_save_to_json_retries_on_permission_error(self):
        from unittest.mock import patch

        book = IronFlyBook(day='2026-10-02', mode='PAPER')
        path = os.path.join(self.tmp, 'iron_fly_retry.json')

        call_count = 0
        real_replace = os.replace

        def mock_replace(src, dst):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise PermissionError(13, 'Permission denied (mock lock)')
            return real_replace(src, dst)

        with patch('os.replace', side_effect=mock_replace):
            book.save_to_json(path)

        self.assertGreaterEqual(call_count, 3)
        self.assertTrue(os.path.isfile(path))

    def test_save_to_json_fallback_copyfile_on_persistent_permission_error(self):
        from unittest.mock import patch

        book = IronFlyBook(day='2026-10-02', mode='PAPER')
        path = os.path.join(self.tmp, 'iron_fly_fallback.json')

        def mock_replace(src, dst):
            raise PermissionError(13, 'Access is denied (mock persistent lock)')

        with patch('os.replace', side_effect=mock_replace):
            book.save_to_json(path)

        self.assertTrue(os.path.isfile(path))
        loaded = IronFlyBook.load_from_json(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.day, '2026-10-02')


if __name__ == '__main__':
    unittest.main()

