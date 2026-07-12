"""Entry monitor claims manual rows atomically."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.manual_helpers import append_manual_session_row
from blocks.session.plan import SessionPlan


class TestManualEntryClaim(unittest.TestCase):
    def test_entry_monitor_claims_manual_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan, row = append_manual_session_row(
                tmp,
                side='P',
                short_strike=7325,
                long_strike=7300,
                limit_credit=0.65,
                quantity=2,
            )
            runner = EntryMonitorRunner(root=tmp)
            now = datetime(2026, 6, 26, 14, 0, 0)
            with patch.object(runner, '_run_worker'):
                with patch('blocks.entry.runner.evaluate_new_risk_gate') as mock_gate:
                    from common.trading_gate import GateDecision
                    mock_gate.return_value = GateDecision(blocked=False)
                    runner.tick(now)
            reloaded = SessionPlan.load(plan.path, strategy=plan.strategy)
            saved = reloaded.row_by_slot_key(row.slot_key)
            self.assertEqual(saved.state, 'placing')
            self.assertIn(row.slot_key, runner._fired)


if __name__ == '__main__':
    unittest.main()
