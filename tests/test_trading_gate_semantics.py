"""Trading gate and dashboard semantics."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from common import broker_cooldown
from common.rest_probe import RestProbeResult
from common.trading_gate import (
    effective_new_risk_blocked,
    initialize_for_session_date,
    latch_new_risk,
    read_state,
    record_probe_result,
    resume_new_risk,
    summary_for_dashboard,
)
from blocks.session.manual_helpers import dispatch_manual_place
from blocks.session.plan import SessionPlan


class TestTradingGateSemantics(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.gate_path = os.path.join(self._tmp.name, 'trading_gate.json')
        self.cooldown_path = os.path.join(self._tmp.name, 'broker_cooldown.json')
        os.environ['MEIC_TRADING_GATE_PATH'] = self.gate_path
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = self.cooldown_path
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        broker_cooldown.clear_cooldown()
        initialize_for_session_date('2026-07-10')

    def tearDown(self):
        self._tmp.cleanup()
        broker_cooldown.clear_cooldown()

    def test_startup_unknown_blocks(self):
        self.assertTrue(effective_new_risk_blocked())
        self.assertEqual(read_state()['rest_status'], 'unknown')

    def test_recheck_success_clears_cooldown_not_latch(self):
        latch_new_risk('incident', source='test')
        broker_cooldown.set_cooldown('429', source='test')
        now = time.time()
        from common.rest_probe import run_rest_probe
        broker = MagicMock()
        broker.probe_orders_rest.return_value = RestProbeResult(
            ok=True, status='healthy',
            attempted_at_epoch=now, completed_at_epoch=now,
            latency_ms=40, http_status=200, detail='',
        )
        run_rest_probe(broker, source='dashboard', bypass_local_cooldown=True)
        self.assertFalse(broker_cooldown.cooldown_active())
        self.assertTrue(read_state()['new_risk_latched'])
        self.assertEqual(read_state()['rest_status'], 'healthy')

    def test_resume_requires_fresh_probe(self):
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True, status='healthy',
            attempted_at_epoch=now - 120, completed_at_epoch=now - 120,
            latency_ms=40, http_status=200, detail='',
        ))
        latch_new_risk('incident', source='test')
        decision = resume_new_risk()
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, 'stale_probe')

    def test_resume_rejected_while_cooldown(self):
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True, status='healthy',
            attempted_at_epoch=now, completed_at_epoch=now,
            latency_ms=40, http_status=200, detail='',
        ))
        latch_new_risk('incident', source='test')
        broker_cooldown.set_cooldown('429', source='test')
        decision = resume_new_risk()
        self.assertTrue(decision.blocked)

    def test_resume_rejected_with_cooldown_blind_trade(self):
        active = os.path.join(self._tmp.name, 'trades', 'active', 'MEIC_IC')
        os.makedirs(active, exist_ok=True)
        trade_path = os.path.join(active, '11-00_P_test.json')
        with open(trade_path, 'w', encoding='utf-8') as f:
            import json
            json.dump({
                'entry_control': 'cooldown_blind',
                'open_order': {'status': 'visibility_unknown'},
            }, f)
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True, status='healthy',
            attempted_at_epoch=now, completed_at_epoch=now,
            latency_ms=40, http_status=200, detail='',
        ))
        latch_new_risk('incident', source='test')
        with patch('common.trading_gate.has_unresolved_visibility_unknown', return_value=True):
            decision = resume_new_risk()
        self.assertTrue(decision.blocked)
        self.assertEqual(decision.reason, 'visibility_unknown_active')

    def test_manual_place_423_before_row_created(self):
        with patch('blocks.session.manual_helpers.is_after_market_close_ct', return_value=False), \
             patch('common.trading_gate.effective_new_risk_blocked', return_value=True), \
             patch('common.trading_gate.gate_enabled', return_value=True), \
             patch('common.trading_gate.summary_for_dashboard', return_value={'reason': 'latched'}), \
             patch('blocks.session.manual_helpers.append_manual_session_row') as mock_append:
            result, code = dispatch_manual_place(
                self._tmp.name,
                launcher_active=True,
                side='P',
                short_strike=7525,
                long_strike=7500,
                limit_credit=1.0,
            )
        self.assertEqual(code, 423)
        mock_append.assert_not_called()

    def test_dashboard_summary_zero_broker_calls(self):
        with patch('dashboard.server._meic_session_rows', return_value=None), \
             patch('dashboard.server._read_active_trades', return_value=[]), \
             patch('dashboard.server.read_bot_status', return_value={}), \
             patch('dashboard.server.build_manual_trades', return_value=([], 0.0, 0, 0.0)), \
             patch('dashboard.server.live_prices', {}):
            from dashboard.server import build_summary
            build_summary()
        # No broker factory calls — summary is file-only
        with patch('common.broker_factory.get_broker') as mock_get:
            from dashboard.server import build_summary
            with patch('dashboard.server._meic_session_rows', return_value=None), \
                 patch('dashboard.server._read_active_trades', return_value=[]), \
                 patch('dashboard.server.read_bot_status', return_value={}), \
                 patch('dashboard.server.build_manual_trades', return_value=([], 0.0, 0, 0.0)), \
                 patch('dashboard.server.live_prices', {}):
                build_summary()
            mock_get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
