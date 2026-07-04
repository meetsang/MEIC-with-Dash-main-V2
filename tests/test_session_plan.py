"""Session CSV plan load/save and bootstrap."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import time

from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionPlan, SessionRow, parse_width
from orchestrator.scheduler import TrancheSlot


class TestSessionPlan(unittest.TestCase):
    def test_bootstrap_creates_twelve_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            self.assertIsNotNone(path)
            plan = SessionPlan.load(path)
            self.assertEqual(len(plan.rows), 2)
            self.assertEqual(plan.rows[0].slot_key, '11-00_P')
            self.assertEqual(plan.rows[1].slot_key, '11-00_C')
            self.assertEqual(bootstrap_meic_session_if_missing(tmp), None)

    def test_update_row_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('02-00', time(13, 59), time(14, 5))],
            )
            from blocks.session.plan import load_meic_session_today

            plan = load_meic_session_today(tmp)
            plan.update_row('02-00_P', paused=True, quantity=3)
            plan.save()
            reloaded = SessionPlan.load(plan.path)
            row = reloaded.row_by_slot_key('02-00_P')
            self.assertTrue(row.paused)
            self.assertEqual(row.quantity, 3)

    def test_parse_width(self):
        self.assertEqual(parse_width('25-35'), (25, 35))
        self.assertEqual(parse_width('30'), (30, 30))

    def test_bulk_update_pending_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[
                    TrancheSlot('11-00', time(10, 59), time(11, 5)),
                    TrancheSlot('12-00', time(11, 59), time(12, 5)),
                ],
            )
            from blocks.session.plan import load_meic_session_today

            plan = load_meic_session_today(tmp)
            bulk_fields = {
                'quantity': 2,
                'width': '30-40',
                'credit_min': 0.60,
                'credit_max': 1.40,
                'stop_multiplier': 3,
                'chase1_max': 5,
            }
            for row in plan.rows:
                if row.state in ('pending', 'entering'):
                    plan.update_row(row.slot_key, **bulk_fields)
            plan.save()
            reloaded = SessionPlan.load(plan.path)
            for row in reloaded.rows:
                self.assertEqual(row.quantity, 2)
                self.assertEqual(row.width, '30-40')
                self.assertAlmostEqual(row.credit_min, 0.60)
                self.assertEqual(row.stop_multiplier, 3)
                self.assertEqual(row.chase1_max, 5)


if __name__ == '__main__':
    unittest.main()
