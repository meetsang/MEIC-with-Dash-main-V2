"""Trading gate state tests."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

from common import broker_cooldown
from common.trading_gate import (
    REST_HEALTHY,
    effective_new_risk_blocked,
    initialize_for_session_date,
    latch_new_risk,
    read_state,
    record_probe_result,
    resume_new_risk,
)
from common.rest_probe import RestProbeResult


class TestTradingGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        gate_path = os.path.join(self._tmp.name, 'trading_gate.json')
        cooldown_path = os.path.join(self._tmp.name, 'broker_cooldown.json')
        os.environ['MEIC_TRADING_GATE_PATH'] = gate_path
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = cooldown_path
        os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
        broker_cooldown.clear_cooldown()

    def tearDown(self):
        self._tmp.cleanup()
        broker_cooldown.clear_cooldown()

    def test_missing_gate_blocks_new_risk(self):
        initialize_for_session_date('2026-07-10')
        self.assertTrue(effective_new_risk_blocked())

    def test_healthy_probe_without_latch_permits(self):
        initialize_for_session_date('2026-07-10')
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True,
            status=REST_HEALTHY,
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=50,
            http_status=200,
            detail='',
        ))
        self.assertFalse(effective_new_risk_blocked())

    def test_healthy_probe_with_latch_still_blocked(self):
        initialize_for_session_date('2026-07-10')
        latch_new_risk('test_latch', source='test')
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True,
            status=REST_HEALTHY,
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=50,
            http_status=200,
            detail='',
        ))
        self.assertTrue(effective_new_risk_blocked())

    def test_429_sets_latch(self):
        initialize_for_session_date('2026-07-10')
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=False,
            status='rate_limited',
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=50,
            http_status=429,
            detail='HTTP 429',
        ))
        state = read_state()
        self.assertEqual(state['rest_status'], 'rate_limited')
        self.assertTrue(state['new_risk_latched'])

    def test_cooldown_blocks_even_if_gate_stale(self):
        initialize_for_session_date('2026-07-10')
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True,
            status=REST_HEALTHY,
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=50,
            http_status=200,
            detail='',
        ))
        broker_cooldown.set_cooldown('429', source='test')
        self.assertTrue(effective_new_risk_blocked())

    def test_resume_clears_latch_when_healthy(self):
        initialize_for_session_date('2026-07-10')
        now = time.time()
        record_probe_result(RestProbeResult(
            ok=True,
            status=REST_HEALTHY,
            attempted_at_epoch=now,
            completed_at_epoch=now,
            latency_ms=50,
            http_status=200,
            detail='',
        ))
        latch_new_risk('manual_test', source='test')
        with patch('common.trading_gate.has_unresolved_visibility_unknown', return_value=False):
            decision = resume_new_risk(cleared_by='test')
        self.assertFalse(decision.blocked)
        self.assertFalse(read_state()['new_risk_latched'])


if __name__ == '__main__':
    unittest.main()
