"""Manual place dispatch — avoid duplicate broker orders."""
from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from blocks.session.manual_helpers import dispatch_manual_place
from blocks.session.plan import SessionPlan


class TestManualPlaceDispatch(unittest.TestCase):
    def test_launcher_active_queues_without_inline_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('blocks.session.manual_helpers.is_after_market_close_ct', return_value=False):
                with patch('common.trading_gate.effective_new_risk_blocked', return_value=False):
                    with patch('blocks.entry.manual_worker.run_manual_entry_row') as mock_run:
                        result, code = dispatch_manual_place(
                            tmp,
                            launcher_active=True,
                            side='P',
                            short_strike=7325,
                            long_strike=7300,
                            limit_credit=0.65,
                            quantity=2,
                        )
            self.assertEqual(code, 200)
            self.assertEqual(result['status'], 'entering')
            mock_run.assert_not_called()
            plan = SessionPlan.load(result['session_path'])
            row = plan.row_by_slot_key(result['slot_key'])
            self.assertEqual(row.state, 'entering')
            self.assertEqual(row.quantity, 2)

    def test_launcher_inactive_runs_inline_worker(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('blocks.session.manual_helpers.is_after_market_close_ct', return_value=False):
                with patch('common.trading_gate.effective_new_risk_blocked', return_value=False):
                    with patch('blocks.entry.manual_worker.run_manual_entry_row') as mock_run:
                        from blocks.entry.result import EntryWorkerResult

                        mock_run.return_value = EntryWorkerResult(
                            slot_key='ms-1_P',
                            state='entered',
                            trade_path=f'{tmp}/trade.json',
                            api_status='placed',
                            lot='ms-1',
                        )
                        with patch('blocks.session.manual_helpers.apply_entry_result'):
                            result, code = dispatch_manual_place(
                                tmp,
                                launcher_active=False,
                                side='P',
                                short_strike=7325,
                                long_strike=7300,
                                limit_credit=0.65,
                                quantity=1,
                            )
            self.assertEqual(code, 200)
            mock_run.assert_called_once()

    def test_blocked_gate_returns_423(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('blocks.session.manual_helpers.is_after_market_close_ct', return_value=False):
                with patch('common.trading_gate.effective_new_risk_blocked', return_value=True):
                    with patch(
                        'common.trading_gate.summary_for_dashboard',
                        return_value={'reason': 'rest_rate_limited'},
                    ):
                        result, code = dispatch_manual_place(
                            tmp,
                            launcher_active=True,
                            side='P',
                            short_strike=7325,
                            long_strike=7300,
                            limit_credit=0.65,
                        )
            self.assertEqual(code, 423)
            self.assertEqual(result['error'], 'new_risk_gate_blocked')


if __name__ == '__main__':
    unittest.main()
