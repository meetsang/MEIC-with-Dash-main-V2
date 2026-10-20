"""Test that cr and times saved through Session Plan stick for next days."""
from __future__ import annotations

import os
import tempfile
import unittest

from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionPlan, meic_session_path
from blocks.session.session_defaults import load_session_defaults, save_session_defaults


class TestSessionDefaultsPersistence(unittest.TestCase):
    def test_cr_and_times_stick_for_next_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 1. Simulate saving cr and times for a slot (e.g. 10-30_P and 11-00_C)
            save_session_defaults('10-30_P', {
                'credit_min': 1.85,
                'credit_max': 2.25,
                'entry_window_start': '10:15',
                'entry_window_end': '10:45',
            }, root=tmp)

            save_session_defaults('11-00_C', {
                'credit_min': 0.95,
                'credit_max': 1.35,
                'entry_window_start': '11:10',
                'entry_window_end': '11:25',
            }, root=tmp)

            defaults = load_session_defaults(root=tmp)
            self.assertEqual(defaults['10-30_P']['credit_min'], 1.85)
            self.assertEqual(defaults['10-30_P']['entry_window_start'], '10:15')

            # 2. Bootstrap session for a new day
            path = bootstrap_meic_session_if_missing(root=tmp)
            self.assertIsNotNone(path)
            self.assertTrue(os.path.isfile(path))

            # 3. Verify the generated session plan adopted the saved cr and times
            plan = SessionPlan.load(path)
            row_p = plan.row_by_slot_key('10-30_P')
            self.assertIsNotNone(row_p)
            self.assertEqual(row_p.credit_min, 1.85)
            self.assertEqual(row_p.credit_max, 2.25)
            self.assertEqual(row_p.entry_window_start, '10:15')
            self.assertEqual(row_p.entry_window_end, '10:45')

            row_c = plan.row_by_slot_key('11-00_C')
            self.assertIsNotNone(row_c)
            self.assertEqual(row_c.credit_min, 0.95)
            self.assertEqual(row_c.credit_max, 1.35)
            self.assertEqual(row_c.entry_window_start, '11:10')
            self.assertEqual(row_c.entry_window_end, '11:25')

            # Rows without explicit override should retain their standard defaults
            row_unmodified = plan.row_by_slot_key('12-00_P')
            self.assertIsNotNone(row_unmodified)
            self.assertEqual(row_unmodified.entry_window_start, '11:59')
            self.assertEqual(row_unmodified.entry_window_end, '12:05')


if __name__ == '__main__':
    unittest.main()
