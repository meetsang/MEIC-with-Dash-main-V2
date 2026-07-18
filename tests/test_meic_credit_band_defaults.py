"""MEIC credit band default source values (0.60–1.20)."""
from __future__ import annotations

import tempfile
import unittest
from datetime import time
from types import SimpleNamespace
from unittest.mock import patch

import meic0dte.app.config as meic_config
from blocks.entry.config import CreditEntryConfig
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionRow
from orchestrator.scheduler import TrancheSlot


class TestMeicCreditBandDefaults(unittest.TestCase):
  def test_meic_config_constants(self):
    self.assertAlmostEqual(meic_config.CREDIT_MIN, 0.60)
    self.assertAlmostEqual(meic_config.CREDIT_MAX_P, 1.20)
    self.assertAlmostEqual(meic_config.CREDIT_MAX_C, 1.20)

  def test_entry_config_defaults(self):
    cfg = CreditEntryConfig()
    self.assertAlmostEqual(cfg.credit_min, 0.60)
    self.assertAlmostEqual(cfg.credit_max_put, 1.20)
    self.assertAlmostEqual(cfg.credit_max_call, 1.20)

  def test_entry_config_from_meic_config(self):
    cfg = CreditEntryConfig.from_meic_config()
    self.assertAlmostEqual(cfg.credit_min, 0.60)
    self.assertAlmostEqual(cfg.credit_max_put, 1.20)
    self.assertAlmostEqual(cfg.credit_max_call, 1.20)

  def test_session_row_defaults(self):
    row = SessionRow(
      slot_key='11-00_P',
      lot='11-00',
      side='P',
      entry_window_start='10:59',
      entry_window_end='11:05',
    )
    self.assertAlmostEqual(row.credit_min, 0.60)
    self.assertAlmostEqual(row.credit_max, 1.20)

  def test_bootstrap_uses_meic_config_defaults(self):
    with tempfile.TemporaryDirectory() as tmp:
      path = bootstrap_meic_session_if_missing(
        tmp,
        slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
      )
      self.assertIsNotNone(path)
      from blocks.session.plan import SessionPlan

      plan = SessionPlan.load(path)
      for row in plan.rows:
        self.assertAlmostEqual(row.credit_min, 0.60)
        self.assertAlmostEqual(row.credit_max, 1.20)

  def test_explicit_csv_override_precedes_defaults(self):
    with tempfile.TemporaryDirectory() as tmp:
      bootstrap_meic_session_if_missing(
        tmp,
        slots=[TrancheSlot('11-00', time(10, 59), time(11, 5))],
      )
      from blocks.session.plan import load_meic_session_today

      plan = load_meic_session_today(tmp)
      plan.update_row('11-00_P', credit_min=0.75, credit_max=1.50)
      plan.save()
      reloaded = load_meic_session_today(tmp)
      row = reloaded.row_by_slot_key('11-00_P')
      self.assertAlmostEqual(row.credit_min, 0.75)
      self.assertAlmostEqual(row.credit_max, 1.50)

  def test_dashboard_fallbacks_when_row_fields_missing(self):
    row = SimpleNamespace(
      slot_key='11-00_P',
      lot='11-00',
      side='P',
      state='pending',
      paused=False,
      skip=False,
      quantity=1,
      width='25-35',
      entry_window_start='10:59',
      entry_window_end='11:05',
      chase1_mode='chase_same_trade',
      chase1_max=3,
      chase2_mode='build_new_strikes',
      chase2_max=7,
      stop_multiplier=2,
      trade_path='',
    )
    plan = SimpleNamespace(rows=[row])
    with patch('dashboard.server._meic_session_rows', return_value=plan), \
         patch('dashboard.server._read_active_trades', return_value=[]), \
         patch('dashboard.server.read_bot_status', return_value={}), \
         patch('dashboard.server.build_manual_trades', return_value=([], 0.0, 0, 0.0)), \
         patch('dashboard.server.live_prices', {}):
      from dashboard.server import build_summary

      summary = build_summary()
    slot = summary['grid'][0]
    self.assertAlmostEqual(slot['credit_min'], 0.60)
    self.assertAlmostEqual(slot['credit_max'], 1.20)

  def test_dashboard_prefers_explicit_row_values_over_fallbacks(self):
    row = SimpleNamespace(
      slot_key='11-00_P',
      lot='11-00',
      side='P',
      state='pending',
      paused=False,
      skip=False,
      quantity=1,
      width='25-35',
      credit_min=0.80,
      credit_max=1.40,
      entry_window_start='10:59',
      entry_window_end='11:05',
      chase1_mode='chase_same_trade',
      chase1_max=3,
      chase2_mode='build_new_strikes',
      chase2_max=7,
      stop_multiplier=2,
      trade_path='',
    )
    plan = SimpleNamespace(rows=[row])
    with patch('dashboard.server._meic_session_rows', return_value=plan), \
         patch('dashboard.server._read_active_trades', return_value=[]), \
         patch('dashboard.server.read_bot_status', return_value={}), \
         patch('dashboard.server.build_manual_trades', return_value=([], 0.0, 0, 0.0)), \
         patch('dashboard.server.live_prices', {}):
      from dashboard.server import build_summary

      summary = build_summary()
    slot = summary['grid'][0]
    self.assertAlmostEqual(slot['credit_min'], 0.80)
    self.assertAlmostEqual(slot['credit_max'], 1.40)


if __name__ == '__main__':
  unittest.main()
