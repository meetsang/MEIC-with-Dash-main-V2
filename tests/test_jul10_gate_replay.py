"""Jul 10 integrated gate replay — duplicate fill prevention."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime, time as dt_time
from unittest.mock import MagicMock, patch

from blocks.entry.meic_worker import run_meic_entry_row
from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import load_meic_session_today
from blocks.stop import state as state_mod
from brokers.base import OrderResult
from brokers.tastytrade_broker import BrokerCooldownActive
from common import broker_cooldown
from common.trading_gate import GateDecision, initialize_for_session_date, latch_new_risk, read_state
from orchestrator.scheduler import TrancheSlot


class _Jul10Broker:
  """Places once; first direct status read fails with cooldown."""

  def __init__(self):
    self.place_calls = 0
    self.cancel_calls = 0
    self.direct_status_calls = 0
    self.order_id = '482624006'

  def place_spread_order(self, short_symbol, long_symbol, qty, credit):
    self.place_calls += 1
    return OrderResult(True, self.order_id, 'working')

  def get_order_status_direct(self, order_id, **kwargs):
    self.direct_status_calls += 1
    raise BrokerCooldownActive('HTTP 429 simulated')

  def get_order_status(self, order_id, **kwargs):
    return self.get_order_status_direct(order_id, **kwargs)

  def cancel_order(self, order_id):
    self.cancel_calls += 1
    return OrderResult(True, order_id, 'cancelled')

  def find_working_open_spread_orders(self, short_symbol, long_symbol):
    return []

  def inspect_spread_position(self, short_symbol, long_symbol, expected_qty=1):
    return 'flat'


class TestJul10GateReplay(unittest.TestCase):
  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.root = self._tmp.name
    os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(self.root, 'trading_gate.json')
    os.environ['MEIC_BROKER_COOLDOWN_PATH'] = os.path.join(self.root, 'broker_cooldown.json')
    os.environ['NEW_RISK_GATE_ENABLED'] = 'true'
    os.environ['TRADES_ACTIVE_DIR'] = os.path.join(self.root, 'trades', 'active', 'MEIC_IC')
    os.makedirs(os.environ['TRADES_ACTIVE_DIR'], exist_ok=True)
    broker_cooldown.clear_cooldown()
    initialize_for_session_date('2026-07-10')
    now = time.time()
    from common.rest_probe import RestProbeResult
    from common.trading_gate import record_probe_result
    record_probe_result(RestProbeResult(
      ok=True, status='healthy',
      attempted_at_epoch=now, completed_at_epoch=now,
      latency_ms=50, http_status=200, detail='',
    ))

  def tearDown(self):
    self._tmp.cleanup()
    broker_cooldown.clear_cooldown()

  def test_jul10_incident_replay_no_duplicate_openings(self):
    bootstrap_meic_session_if_missing(
      self.root,
      slots=[TrancheSlot('11-00', dt_time(10, 59), dt_time(11, 5))],
    )
    plan = load_meic_session_today(self.root)
    row_p = plan.row_by_slot_key('11-00_P')
    broker = _Jul10Broker()

    with patch('blocks.entry.meic_worker.get_shared_broker', return_value=broker), \
         patch('blocks.entry.meic_worker._scan_pick') as mock_scan, \
         patch('blocks.entry.meic_worker.util.get_expiration_date', return_value='260710'), \
         patch('blocks.entry.meic_worker.register_spread_symbols'):
      from blocks.entry.meic_worker import _StrikePick
      mock_scan.return_value = _StrikePick(
        '.SPXW260710P7525', '.SPXW260710P7500', 7525, 7500, 1.0,
      )
      result = run_meic_entry_row(row_p)

    self.assertEqual(result.error, 'cooldown_blind')
    self.assertEqual(broker.place_calls, 1)
    self.assertEqual(broker.cancel_calls, 0)
    self.assertTrue(result.trade_path)
    self.assertTrue(os.path.isfile(result.trade_path))

    st = state_mod.load_state(result.trade_path)
    self.assertEqual(st.get('entry_control'), 'cooldown_blind')
    self.assertEqual((st.get('open_order') or {}).get('status'), 'visibility_unknown')
    self.assertTrue(read_state().get('new_risk_latched'))

    broker_cooldown.set_cooldown('HTTP 429', source='jul10_replay')
    sibling_spawned = {'n': 0}
    runner = EntryMonitorRunner(root=self.root)
    real_run = runner._run_worker

    def _track_worker(*args, **kwargs):
      if args[1] == '11-00_C':
        sibling_spawned['n'] += 1
      return real_run(*args, **kwargs)

    now = datetime(2026, 7, 10, 11, 0, 0)
    with patch.object(runner, '_run_worker', side_effect=_track_worker):
      runner.tick(now)

    self.assertEqual(sibling_spawned['n'], 0)
    self.assertNotIn('11-00_C', runner._fired)
    plan2 = load_meic_session_today(self.root)
    self.assertEqual(plan2.row_by_slot_key('11-00_C').state, 'pending')
    self.assertEqual(broker.place_calls, 1)


if __name__ == '__main__':
  unittest.main()
