"""Tests for MEIC aggressive credit skew, dynamic 15c OCO flag, side toggles, and new schedule."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import MagicMock, patch

from blocks.entry.runner import EntryMonitorRunner
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.plan import SessionPlan, SessionRow
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.profiles.meic import meic_stop_profile
from common import trades_layout
from common.meic_settings import (
    MeicSettings,
    get_meic_settings,
    get_meic_tranche_credits,
    is_15c_oco_enabled,
    is_companion_tightening_enabled,
    is_meic_side_enabled,
    reconcile_meic_active_stops,
    save_meic_settings,
)
from orchestrator.scheduler import TrancheSlot
from strategies.meic.strategy import MEIC_TRANCHE_SLOTS

try:
    from tests.mock_broker import MockBroker
except ImportError:
    from mock_broker import MockBroker


class TestMeicAggressiveAndOcoSettings(unittest.TestCase):
    def test_credit_calculations_first_four_vs_last_two(self):
        # Default settings: aggressive_side = 'P', aggressive_pct = 0.25
        settings = MeicSettings(aggressive_side='P', aggressive_pct=0.25)

        # First 4 tranches: 10-30, 11-00, 12-00, 01-00
        # Base: (1.20, 1.60) -> Put +25% = (1.50, 2.00)
        for lot in ('10-30', '11-00', '12-00', '01-00'):
            p_min, p_max = get_meic_tranche_credits(lot, 'P', settings=settings)
            c_min, c_max = get_meic_tranche_credits(lot, 'C', settings=settings)
            self.assertEqual((p_min, p_max), (1.50, 2.00))
            self.assertEqual((c_min, c_max), (1.20, 1.60))

        # Last 2 tranches: 01-15, 02-15
        # Base: (0.50, 1.00) -> Put +25% = (0.62, 1.25)
        for lot in ('01-15', '02-15'):
            p_min, p_max = get_meic_tranche_credits(lot, 'P', settings=settings)
            c_min, c_max = get_meic_tranche_credits(lot, 'C', settings=settings)
            self.assertEqual((p_min, p_max), (0.62, 1.25))
            self.assertEqual((c_min, c_max), (0.50, 1.00))

    def test_credit_calculations_call_skew_and_balanced(self):
        # Call aggressive
        call_cfg = MeicSettings(aggressive_side='C', aggressive_pct=0.25)
        p_min, p_max = get_meic_tranche_credits('10-30', 'P', settings=call_cfg)
        c_min, c_max = get_meic_tranche_credits('10-30', 'C', settings=call_cfg)
        self.assertEqual((p_min, p_max), (1.20, 1.60))
        self.assertEqual((c_min, c_max), (1.50, 2.00))

        # Balanced (NONE)
        balanced_cfg = MeicSettings(aggressive_side='NONE', aggressive_pct=0.25)
        p_min, p_max = get_meic_tranche_credits('10-30', 'P', settings=balanced_cfg)
        c_min, c_max = get_meic_tranche_credits('10-30', 'C', settings=balanced_cfg)
        self.assertEqual((p_min, p_max), (1.20, 1.60))
        self.assertEqual((c_min, c_max), (1.20, 1.60))

    def test_session_plan_bootstrapping_schedule_and_bands(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = bootstrap_meic_session_if_missing(tmp)
            self.assertIsNotNone(path)
            plan = SessionPlan.load(path)

            # Expect 12 rows (6 lots x 2 sides)
            self.assertEqual(len(plan.rows), 12)
            lots = {r.lot for r in plan.rows}
            expected_lots = {'10-30', '11-00', '12-00', '01-00', '01-15', '02-15'}
            self.assertEqual(lots, expected_lots)

            # Check credit bands on rows
            for row in plan.rows:
                self.assertFalse(row.paused)
                if row.lot in ('10-30', '11-00', '12-00', '01-00'):
                    if row.side == 'P':
                        self.assertEqual((row.credit_min, row.credit_max), (1.50, 2.00))
                    else:
                        self.assertEqual((row.credit_min, row.credit_max), (1.20, 1.60))
                else:
                    if row.side == 'P':
                        self.assertEqual((row.credit_min, row.credit_max), (0.62, 1.25))
                    else:
                        self.assertEqual((row.credit_min, row.credit_max), (0.50, 1.00))

    def test_side_toggle_controls_runner_firing(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = EntryMonitorRunner(root=tmp)
            # Create settings where CCS is disabled
            settings = MeicSettings(side_ccs_enabled=False, side_pcs_enabled=True)
            save_meic_settings(settings, root=tmp)

            call_row = SessionRow(
                slot_key='10-30_C',
                lot='10-30',
                side='C',
                entry_window_start='10:29',
                entry_window_end='10:35',
                state='pending',
                paused=False,
            )
            put_row = SessionRow(
                slot_key='10-30_P',
                lot='10-30',
                side='P',
                entry_window_start='10:29',
                entry_window_end='10:35',
                state='pending',
                paused=False,
            )

            now_time = time(10, 30)
            now_dt = datetime(2026, 9, 23, 10, 30)

            # CCS is disabled -> call row should not fire
            self.assertFalse(runner._should_fire_meic(call_row, now_time, now_dt))
            # PCS is enabled -> put row should fire
            self.assertTrue(runner._should_fire_meic(put_row, now_time, now_dt))

            # Enable CCS and disable PCS
            settings.side_ccs_enabled = True
            settings.side_pcs_enabled = False
            save_meic_settings(settings, root=tmp)

            self.assertTrue(runner._should_fire_meic(call_row, now_time, now_dt))
            self.assertFalse(runner._should_fire_meic(put_row, now_time, now_dt))

    def test_reconcile_active_stops_switch_between_stop_and_oco(self):
        broker = MockBroker()
        with tempfile.TemporaryDirectory() as tmp:
            active_dir = os.path.join(tmp, 'trades', 'active', 'MEIC_IC')
            os.makedirs(active_dir, exist_ok=True)

            trade_path = os.path.join(active_dir, '10-30_P_20260923T103000.json')
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='10-30',
                side='P',
                short_symbol='.SPXW260923P5400',
                long_symbol='.SPXW260923P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.00,
                long_fill=0.10,
                net_credit=0.90,
                quantity=1,
                open_order_id='open_1',
            )
            # Starts with normal STOP_LIMIT
            st['active_stop'] = {
                'order_id': 'stop_101',
                'type': 'STOP_LIMIT',
                'stop_price': 1.80,
                'limit_price': 1.90,
                'profit_price': None,
                'phase': 1,
                'status': 'working',
                'quantity': 1,
            }
            state_mod.save_state(trade_path, st)

            # Switch to OCO
            modified = reconcile_meic_active_stops(broker, desired_oco=True, root=tmp)
            self.assertEqual(len(modified), 1)

            reloaded = state_mod.load_state(trade_path)
            active = reloaded.get('active_stop') or {}
            self.assertEqual(active.get('type'), 'OCO')
            self.assertAlmostEqual(active.get('profit_price'), 0.15)
            self.assertAlmostEqual(active.get('stop_price'), 1.80)
            self.assertAlmostEqual(active.get('limit_price'), 1.90)

            # Switch back to STOP_LIMIT
            modified = reconcile_meic_active_stops(broker, desired_oco=False, root=tmp)
            self.assertEqual(len(modified), 1)

            reloaded = state_mod.load_state(trade_path)
            active = reloaded.get('active_stop') or {}
            self.assertEqual(active.get('type'), 'STOP_LIMIT')
            self.assertIsNone(active.get('profit_price'))
            self.assertAlmostEqual(active.get('stop_price'), 1.80)

    def test_monitor_initial_stop_uses_normal_stop_by_default(self):
        broker = MockBroker()
        prices = MagicMock(spec=MqttPriceCache)
        prices.get = lambda sym: broker.prices.get(sym)
        prices.get_market_mid = lambda sym: broker.prices.get(sym)
        prices.get_spx = lambda: 5500.0
        prices.kill_switch = False

        with patch.object(StopMonitor, '_broker_actions_blocked', return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                # Ensure 15c OCO is OFF (default)
                save_meic_settings(MeicSettings(use_oco_15c=False), root=tmp)

                st = state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-30',
                    side='P',
                    short_symbol='.SPXW260923P5400',
                    long_symbol='.SPXW260923P5370',
                    short_strike=5400,
                    long_strike=5370,
                    short_fill=1.00,
                    long_fill=0.10,
                    net_credit=0.90,
                    quantity=1,
                    open_order_id='open_1',
                )
                path = os.path.join(tmp, '10-30_P_20260923T103000.json')
                state_mod.save_state(path, st)

                mon = StopMonitor(path, broker, prices=prices)
                mon.setup_initial_stop(stop_multiplier=2.0)

                active = mon.state.get('active_stop') or {}
                self.assertEqual(active.get('type'), 'STOP_LIMIT')
                self.assertIsNone(active.get('profit_price'))
                self.assertAlmostEqual(active.get('stop_price'), 1.80)
                self.assertAlmostEqual(active.get('limit_price'), 1.90)

    def test_monitor_reconciles_order_type_mismatch_in_tick(self):
        broker = MockBroker()
        prices = MagicMock(spec=MqttPriceCache)
        prices.get = lambda sym: broker.prices.get(sym)
        prices.get_market_mid = lambda sym: broker.prices.get(sym)
        prices.get_spx = lambda: 5500.0
        prices.kill_switch = False

        with patch.object(StopMonitor, '_broker_actions_blocked', return_value=False):
            with tempfile.TemporaryDirectory() as tmp:
                # Flag is turned ON
                save_meic_settings(MeicSettings(use_oco_15c=True), root=tmp)

                st = state_mod.create_new_state(
                    strategy='MEIC_IC',
                    lot='10-30',
                    side='P',
                    short_symbol='.SPXW260923P5400',
                    long_symbol='.SPXW260923P5370',
                    short_strike=5400,
                    long_strike=5370,
                    short_fill=1.00,
                    long_fill=0.10,
                    net_credit=0.90,
                    quantity=1,
                    open_order_id='open_1',
                )
                # Trade is open and currently has a STOP_LIMIT order
                st['active_stop'] = {
                    'order_id': 'old_stop_1',
                    'type': 'STOP_LIMIT',
                    'stop_price': 1.80,
                    'limit_price': 1.90,
                    'profit_price': None,
                    'phase': 1,
                    'status': 'working',
                    'quantity': 1,
                }
                path = os.path.join(tmp, '10-30_P_20260923T103000.json')
                state_mod.save_state(path, st)

                mon = StopMonitor(path, broker, prices=prices)
                # Ensure stop check runs
                mon._ensure_stop_for_filled_qty_unblocked()

                active = mon.state.get('active_stop') or {}
                # Should have been reconciled to OCO
                self.assertEqual(active.get('type'), 'OCO')
                self.assertAlmostEqual(active.get('profit_price'), 0.15)
                self.assertAlmostEqual(active.get('stop_price'), 1.80)


    def test_dashboard_api_meic_settings(self):
        from dashboard.server import app, ROOT
        with tempfile.TemporaryDirectory() as tmp:
            with patch('dashboard.server.ROOT', tmp):
                bootstrap_meic_session_if_missing(tmp)
                client = app.test_client()

                # 1. GET settings
                resp = client.get('/api/meic/settings')
                self.assertEqual(resp.status_code, 200)
                data = resp.get_json()
                self.assertEqual(data['status'], 'ok')
                self.assertFalse(data['settings']['use_oco_15c'])
                self.assertFalse(data['settings']['companion_breach_tightening'])
                self.assertEqual(data['settings']['aggressive_side'], 'P')

                # 2. POST update: toggle OCO on, disable CCS, change aggressive to Call, toggle companion tightening on
                post_resp = client.post(
                    '/api/meic/settings',
                    json={
                        'use_oco_15c': True,
                        'side_ccs_enabled': False,
                        'aggressive_side': 'C',
                        'companion_breach_tightening': True,
                    },
                )
                self.assertEqual(post_resp.status_code, 200)
                post_data = post_resp.get_json()
                self.assertEqual(post_data['status'], 'ok')
                self.assertTrue(post_data['settings']['use_oco_15c'])
                self.assertFalse(post_data['settings']['side_ccs_enabled'])
                self.assertEqual(post_data['settings']['aggressive_side'], 'C')
                self.assertTrue(post_data['settings']['companion_breach_tightening'])

                # 3. Verify session plan sync
                from blocks.session.plan import load_meic_session_today
                plan = load_meic_session_today(tmp)
                self.assertIsNotNone(plan)
                c_row = plan.row_by_slot_key('10-30_C')
                p_row = plan.row_by_slot_key('10-30_P')

                # CCS disabled -> Call row should be paused
                self.assertTrue(c_row.paused)
                # PCS enabled -> Put row should not be paused
                self.assertFalse(p_row.paused)

                # Call aggressive -> Call row has +25% skew (1.50 - 2.00)
                self.assertEqual((c_row.credit_min, c_row.credit_max), (1.50, 2.00))
                self.assertEqual((p_row.credit_min, p_row.credit_max), (1.20, 1.60))

    def test_companion_breach_tightening_setting_and_env(self):
        # Default should be False (off normally)
        with tempfile.TemporaryDirectory() as tmp:
            st = get_meic_settings(root=tmp)
            self.assertFalse(st.companion_breach_tightening)
            self.assertFalse(is_companion_tightening_enabled(root=tmp))

            # Saving as True updates setting
            save_meic_settings(MeicSettings(companion_breach_tightening=True), root=tmp)
            st2 = get_meic_settings(root=tmp)
            self.assertTrue(st2.companion_breach_tightening)
            self.assertTrue(is_companion_tightening_enabled(root=tmp))

            # Environment variable overrides
            with patch.dict(os.environ, {'MEIC_COMPANION_BREACH_TIGHTENING': '0'}):
                self.assertFalse(is_companion_tightening_enabled(root=tmp))
            with patch.dict(os.environ, {'MEIC_COMPANION_BREACH_TIGHTENING': '1'}):
                self.assertTrue(is_companion_tightening_enabled(root=tmp))


if __name__ == '__main__':
    unittest.main()
