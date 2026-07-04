"""Paper-day gates and session cutover cleanup."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import patch

from blocks.entry.fire_lot import fire_meic_lot
from blocks.entry.result import EntryWorkerResult
from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import load_meic_session_today
from orchestrator.scheduler import TrancheSlot


class TestFireMeicLot(unittest.TestCase):
    def test_fires_csv_rows_for_lot(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            def _fake_entry(row, row_log=None):
                return EntryWorkerResult(slot_key=row.slot_key, state='entered', lot=row.lot)

            with patch('blocks.entry.fire_lot.run_meic_entry_row', side_effect=_fake_entry):
                ok = fire_meic_lot(tmp, '11-00')
            self.assertTrue(ok)

            plan = load_meic_session_today(tmp)
            self.assertEqual(plan.row_by_slot_key('11-00_P').state, 'entered')
            self.assertEqual(plan.row_by_slot_key('11-00_C').state, 'entered')

    def test_skips_paused_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            plan.update_row('11-00_P', paused=True)
            plan.save()
            def _fake_entry(row, row_log=None):
                return EntryWorkerResult(slot_key=row.slot_key, state='entered', lot=row.lot)

            with patch('blocks.entry.fire_lot.run_meic_entry_row', side_effect=_fake_entry) as mock_run:
                fire_meic_lot(tmp, '11-00')
            self.assertEqual(mock_run.call_count, 1)

    def test_returns_false_when_no_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            self.assertFalse(fire_meic_lot(tmp, 'integration-session'))


class TestPauseCsvOnly(unittest.TestCase):
    def test_pause_api_does_not_write_pause_tranches_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            trades_dir = os.path.join(tmp, 'trades')
            os.makedirs(trades_dir, exist_ok=True)
            pause_path = os.path.join(trades_dir, 'pause_tranches.json')

            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            plan.update_row('11-00_P', paused=True)
            plan.save()

            self.assertFalse(os.path.isfile(pause_path))

            runner = EntryMonitorRunner(root=tmp)
            with patch.object(runner, '_run_worker'):
                runner.tick(datetime(2026, 6, 25, 11, 0, 0))
            self.assertNotIn('11-00_P', runner._fired)


if __name__ == '__main__':
    unittest.main()
