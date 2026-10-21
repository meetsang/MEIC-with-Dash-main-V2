"""Session CSV concurrent update safety."""
from __future__ import annotations

import tempfile
import threading
import unittest

from blocks.entry.result import EntryWorkerResult
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.csv_update import apply_entry_result, mark_row_entering
from blocks.session.plan import SessionPlan, load_meic_session_today
from orchestrator.scheduler import TrancheSlot


class TestCsvUpdate(unittest.TestCase):
    def test_concurrent_p_and_c_both_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', __import__('datetime').time(10, 59), __import__('datetime').time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            mark_row_entering(plan.path, '11-00_P', strategy=plan.strategy)
            mark_row_entering(plan.path, '11-00_C', strategy=plan.strategy)

            path_p = f'{tmp}/trades/active/MEIC_IC/11-00_P_test.json'
            path_c = f'{tmp}/trades/active/MEIC_IC/11-00_C_test.json'

            def apply_p():
                apply_entry_result(
                    plan.path,
                    EntryWorkerResult(slot_key='11-00_P', state='entered', trade_path=path_p, lot='11-00'),
                    strategy=plan.strategy,
                )

            def apply_c():
                apply_entry_result(
                    plan.path,
                    EntryWorkerResult(slot_key='11-00_C', state='entered', trade_path=path_c, lot='11-00'),
                    strategy=plan.strategy,
                )

            t1 = threading.Thread(target=apply_p)
            t2 = threading.Thread(target=apply_c)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            final = SessionPlan.load(plan.path, strategy=plan.strategy)
            p_row = final.row_by_slot_key('11-00_P')
            c_row = final.row_by_slot_key('11-00_C')
            self.assertEqual(p_row.state, 'entered')
            self.assertEqual(p_row.trade_path, path_p)
            self.assertEqual(c_row.state, 'entered')
            self.assertEqual(c_row.trade_path, path_c)

    def test_second_apply_does_not_clobber_first(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', __import__('datetime').time(10, 59), __import__('datetime').time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            path_p = f'{tmp}/p.json'
            path_c = f'{tmp}/c.json'

            apply_entry_result(
                plan.path,
                EntryWorkerResult(slot_key='11-00_P', state='entered', trade_path=path_p),
                strategy=plan.strategy,
            )
            apply_entry_result(
                plan.path,
                EntryWorkerResult(slot_key='11-00_C', state='entered', trade_path=path_c),
                strategy=plan.strategy,
            )

            final = SessionPlan.load(plan.path, strategy=plan.strategy)
            self.assertEqual(final.row_by_slot_key('11-00_P').trade_path, path_p)
            self.assertEqual(final.row_by_slot_key('11-00_C').trade_path, path_c)

    def test_apply_entry_result_persists_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', __import__('datetime').time(10, 59), __import__('datetime').time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            err_msg = '11-00 C: no in-band credit (empty scan — check API quotes or credit band)'
            apply_entry_result(
                plan.path,
                EntryWorkerResult(slot_key='11-00_C', state='failed', error=err_msg),
                strategy=plan.strategy,
            )

            final = SessionPlan.load(plan.path, strategy=plan.strategy)
            c_row = final.row_by_slot_key('11-00_C')
            self.assertEqual(c_row.state, 'failed')
            self.assertEqual(c_row.error, err_msg)

            # Re-entering should clear error
            mark_row_entering(plan.path, '11-00_C', strategy=plan.strategy)
            entering_plan = SessionPlan.load(plan.path, strategy=plan.strategy)
            self.assertEqual(entering_plan.row_by_slot_key('11-00_C').error, '')


if __name__ == '__main__':
    unittest.main()
