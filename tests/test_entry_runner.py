"""Entry monitor supervisor firing rules."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import patch

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionPlan, load_meic_session_today
from orchestrator.scheduler import TrancheSlot


class TestEntryRunner(unittest.TestCase):
    def test_skips_paused_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            plan.update_row('11-00_P', paused=True)
            plan.save()

            runner = EntryMonitorRunner(root=tmp)
            now = datetime(2026, 6, 25, 11, 0, 0)
            with patch.object(runner, '_run_worker'):
                runner.tick(now)
            self.assertNotIn('11-00_P', runner._fired)

    def test_fires_pending_row_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            runner = EntryMonitorRunner(root=tmp)
            now = datetime(2026, 6, 25, 11, 0, 0)
            with patch.object(runner, '_run_worker'):
                runner.tick(now)
            plan = load_meic_session_today(tmp)
            self.assertEqual(plan.row_by_slot_key('11-00_P').state, 'entering')
            self.assertIn('11-00_P', runner._fired)


if __name__ == '__main__':
    unittest.main()
