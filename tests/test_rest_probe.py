"""REST probe tests."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from common import broker_cooldown
from common.rest_probe import RestProbeResult, classify_rest_exception, run_rest_probe
from common.trading_gate import read_state


class TestRestProbe(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self._tmp.name, 'trading_gate.json')
        os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self._tmp.name, 'broker_cooldown.json')
        os.environ['REST_PROBE_MIN_INTERVAL_SEC'] = '0'
        broker_cooldown.clear_cooldown()
        from common.trading_gate import initialize_for_session_date

        initialize_for_session_date('2026-07-10')

    def tearDown(self):
        self._tmp.cleanup()
        broker_cooldown.clear_cooldown()

    def test_classify_429(self):
        status, code = classify_rest_exception(Exception('HTTP 429 Too Many Requests'))
        self.assertEqual(status, 'rate_limited')
        self.assertEqual(code, 429)

    def test_classify_auth(self):
        status, _ = classify_rest_exception(Exception('401 unauthorized'))
        self.assertEqual(status, 'auth_failed')

    def test_probe_skipped_during_cooldown(self):
        broker_cooldown.set_cooldown('429', source='test')
        broker = MagicMock()
        result = run_rest_probe(broker, source='test', bypass_local_cooldown=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, 'rate_limited')
        broker.probe_orders_rest.assert_not_called()

    def test_probe_one_call_on_success(self):
        now = time.time()
        broker = MagicMock()
        broker.probe_orders_rest.return_value = RestProbeResult(
            ok=True,
            status='healthy',
            attempted_at_epoch=now,
            completed_at_epoch=now + 0.1,
            latency_ms=100,
            http_status=200,
            detail='',
        )
        result = run_rest_probe(broker, source='test', bypass_local_cooldown=True)
        self.assertTrue(result.ok)
        broker.probe_orders_rest.assert_called_once()
        self.assertEqual(read_state()['rest_status'], 'healthy')

    def test_bypass_clears_cooldown_on_success(self):
        broker_cooldown.set_cooldown('429', source='test')
        now = time.time()
        broker = MagicMock()
        broker.probe_orders_rest.return_value = RestProbeResult(
            ok=True,
            status='healthy',
            attempted_at_epoch=now,
            completed_at_epoch=now + 0.1,
            latency_ms=100,
            http_status=200,
            detail='',
        )
        run_rest_probe(broker, source='dashboard', bypass_local_cooldown=True)
        self.assertFalse(broker_cooldown.cooldown_active())
        self.assertTrue(read_state()['new_risk_latched'])


if __name__ == '__main__':
    unittest.main()
