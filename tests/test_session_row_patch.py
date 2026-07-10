"""Dashboard session row PATCH API."""
from __future__ import annotations

import os
import tempfile
import unittest

from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import load_meic_session_today
from dashboard.server import (
    _session_row_patch_allowed,
    _session_row_patch_extras,
)
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

    def test_window_editable_on_failed_resets_pending(self):
        plan = load_meic_session_today(self.tmp)
        plan.update_row('11-00_P', state='failed')
        updates = {'entry_window_start': '10:55', 'entry_window_end': '11:10'}
        self.assertTrue(_session_row_patch_allowed('failed', updates))
        extras = _session_row_patch_extras('failed', updates)
        self.assertEqual(extras.get('state'), 'pending')

    def test_credit_not_editable_on_failed(self):
        updates = {'credit_min': 0.80}
        self.assertFalse(_session_row_patch_allowed('failed', updates))

    def test_lot_window_updates_both_sides(self):
        plan = load_meic_session_today(self.tmp)
        for sk in ('11-00_P', '11-00_C'):
            plan.update_row(sk, entry_window_start='10:59', entry_window_end='11:05', state='pending')
        plan.save()
        for sk in ('11-00_P', '11-00_C'):
            row = plan.row_by_slot_key(sk)
            patch = {'entry_window_start': '11:00', 'entry_window_end': '11:06'}
            patch.update(_session_row_patch_extras(row.state, patch))
            plan.update_row(sk, **patch)
        plan.save()
        self.assertEqual(plan.row_by_slot_key('11-00_P').entry_window_start, '11:00')
        self.assertEqual(plan.row_by_slot_key('11-00_C').entry_window_end, '11:06')


if __name__ == '__main__':
    unittest.main()
