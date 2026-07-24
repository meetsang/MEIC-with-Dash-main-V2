"""Manual session CSV append and entry worker."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.entry.manual_worker import run_manual_entry_row
from blocks.session.csv_update import apply_entry_result
from blocks.session.manual_helpers import append_manual_session_row
from blocks.session.plan import SessionPlan, load_manual_session_today


def _run_manual_with_mocks(tmp, row, broker):
    broker.get_order_status_direct = broker.get_order_status
    patches = [
        patch('blocks.entry.manual_worker.get_shared_broker', return_value=broker),
        patch('blocks.entry.manual_worker.effective_new_risk_blocked', return_value=False),
        patch('blocks.entry.manual_worker._resolve_strikes_for_overlap', return_value=(
            7000, 6975, '.SPXW260625P7000', '.SPXW260625P6975', 0,
        )),
        patch('blocks.entry.manual_worker.register_spread_symbols'),
        patch('blocks.entry.manual_worker.util.get_expiration_date', return_value='260625'),
        patch('blocks.entry.manual_worker.state_mod.manual_spread_active_dir', return_value=tmp),
    ]
    ctx = [p.start() for p in patches]
    try:
        return run_manual_entry_row(row)
    finally:
        for p in patches:
            p.stop()


class TestManualSession(unittest.TestCase):
    def test_append_manual_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp,
                side='P',
                short_strike=7335,
                long_strike=7310,
                limit_credit=0.90,
                quantity=3,
                expiry='2026-06-25',
            )
            self.assertEqual(row.slot_key, f'{row.lot}_P')
            self.assertEqual(row.state, 'entering')
            self.assertEqual(row.on_unfilled, 'none')
            reloaded = load_manual_session_today(tmp)
            self.assertEqual(len(reloaded.rows), 1)
            self.assertEqual(reloaded.rows[0].short_strike, 7335)

    def test_append_manual_row_with_plan_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp,
                side='P',
                short_strike=7335,
                long_strike=7310,
                limit_credit=0.90,
                quantity=3,
                expiry='2026-06-25',
                stop_multiplier=3,
                on_unfilled='chase_same_trade',
                chase_floor=0.45,
                chase_max_attempts=3,
            )
            self.assertEqual(row.stop_multiplier, 3)
            self.assertEqual(row.on_unfilled, 'chase_same_trade')
            self.assertEqual(row.credit_min, 0.45)
            self.assertEqual(row.chase1_max, 3)

    def test_manual_worker_writes_plan_to_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp, side='P', short_strike=7000, long_strike=6975, limit_credit=1.0,
                quantity=1, stop_multiplier=3,
            )
            broker = MagicMock()
            broker.place_spread_order.return_value = MagicMock(success=True, order_id='999', message='')
            broker.get_order_status.return_value = MagicMock(
                success=True, status='filled', filled_quantity=1, order_quantity=1,
                filled_price=1.0, short_fill_price=2.0, long_fill_price=1.0,
            )
            result = _run_manual_with_mocks(tmp, row, broker)
            apply_entry_result(plan.path, result, strategy=plan.strategy)
            from blocks.stop import state as state_mod
            reloaded = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved = reloaded.row_by_slot_key(row.slot_key)
            st = state_mod.load_state(saved.trade_path)
            self.assertEqual(st.get('stop_multiplier'), 3)
            self.assertEqual(st['plan']['stop_multiplier'], 3)

    def test_manual_worker_places_and_updates_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp, side='P', short_strike=7000, long_strike=6975, limit_credit=1.0, quantity=1,
            )
            broker = MagicMock()
            broker.place_spread_order.return_value = MagicMock(
                success=True, order_id='999', message='',
            )
            broker.get_order_status.return_value = MagicMock(
                success=True,
                status='filled',
                filled_quantity=1,
                order_quantity=1,
                filled_price=1.0,
                short_fill_price=2.0,
                long_fill_price=1.0,
            )
            result = _run_manual_with_mocks(tmp, row, broker)
            apply_entry_result(plan.path, result, strategy=plan.strategy)

            self.assertIn(result.api_status, ('placed', 'partial', 'working'))
            final = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved = final.row_by_slot_key(row.slot_key)
            self.assertEqual(saved.state, 'entered')
            self.assertTrue(saved.trade_path)


if __name__ == '__main__':
    unittest.main()
