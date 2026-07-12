"""Entry monitor supervisor firing rules."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import patch

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionPlan, load_meic_session_today
from common.trading_gate import GateDecision
from orchestrator.scheduler import TrancheSlot


class TestEntryRunner(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'broker_cooldown.json')
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        from common.trading_gate import initialize_for_session_date

        initialize_for_session_date('2026-06-25')

    def tearDown(self):
        self._tmp.cleanup()

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
                with patch('blocks.entry.runner.evaluate_new_risk_gate', return_value=GateDecision(blocked=False)):
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
                with patch('blocks.entry.runner.evaluate_new_risk_gate', return_value=GateDecision(blocked=False)):
                    runner.tick(now)
            plan = load_meic_session_today(tmp)
            self.assertEqual(plan.row_by_slot_key('11-00_P').state, 'entering')
            self.assertIn('11-00_P', runner._fired)

    def test_blocked_gate_prevents_spawn(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            runner = EntryMonitorRunner(root=tmp)
            now = datetime(2026, 6, 25, 11, 0, 0)
            with patch.object(runner, '_run_worker'):
                with patch(
                    'blocks.entry.runner.evaluate_new_risk_gate',
                    return_value=GateDecision(blocked=True, reason='rest_rate_limited'),
                ):
                    runner.tick(now)
            plan = load_meic_session_today(tmp)
            self.assertEqual(plan.row_by_slot_key('11-00_P').state, 'pending')
            self.assertNotIn('11-00_P', runner._fired)

    def test_refires_after_operator_reset_failed_to_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            bootstrap_meic_session_if_missing(
                tmp,
                slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
            )
            plan = load_meic_session_today(tmp)
            plan.update_row('11-00_P', state='failed')
            plan.save()

            runner = EntryMonitorRunner(root=tmp)
            runner._fired.add('11-00_P')
            plan.update_row('11-00_P', state='pending', entry_window_start='10:59', entry_window_end='11:05')
            plan.save()

            now = datetime(2026, 6, 25, 11, 0, 0)
            with patch.object(runner, '_run_worker'):
                with patch('blocks.entry.runner.evaluate_new_risk_gate', return_value=GateDecision(blocked=False)):
                    runner.tick(now)
            plan = load_meic_session_today(tmp)
            self.assertEqual(plan.row_by_slot_key('11-00_P').state, 'entering')
            self.assertIn('11-00_P', runner._fired)


if __name__ == '__main__':
    unittest.main()
