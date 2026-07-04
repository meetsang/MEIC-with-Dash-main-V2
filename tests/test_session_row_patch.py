"""Dashboard session row PATCH API."""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import load_meic_session_today
from orchestrator.scheduler import TrancheSlot


class TestSessionRowPatch(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        bootstrap_meic_session_if_missing(
            self.tmp,
            slots=[TrancheSlot('11-00', __import__('datetime').time(10, 59), __import__('datetime').time(11, 5))],
        )

    def test_patch_credit_and_window(self):
        plan = load_meic_session_today(self.tmp)
        plan.update_row('11-00_P', credit_min=0.75, credit_max=1.50, entry_window_start='10:55', entry_window_end='11:10')
        plan.save()
        row = plan.row_by_slot_key('11-00_P')
        self.assertEqual(row.credit_min, 0.75)
        self.assertEqual(row.entry_window_start, '10:55')


if __name__ == '__main__':
    unittest.main()
