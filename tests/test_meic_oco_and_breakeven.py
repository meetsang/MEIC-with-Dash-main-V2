"""Unit tests for MEIC OCO profit target (15c) and IC companion breach breakeven tightening."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.companion_breach import find_companion_trade_state, is_companion_breached
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.phases import PhaseCompanionBreachAdjustment
from blocks.stop.profiles.meic import meic_stop_profile
from brokers.base import OrderResult
from common import trades_layout
from common.meic_settings import MeicSettings, save_meic_settings
try:
    from tests.mock_broker import MockBroker
except ImportError:
    from mock_broker import MockBroker


class TestMeicOcoAndBreakeven(unittest.TestCase):
    def setUp(self):
        self.broker = MockBroker()
        self.prices = MagicMock(spec=MqttPriceCache)
        self.prices.get = lambda sym: self.broker.prices.get(sym)
        self.prices.get_market_mid = lambda sym: self.broker.prices.get(sym)
        self.prices.get_spx = lambda: 5500.0
        self.prices.kill_switch = False
        self._blocked_patcher = patch.object(StopMonitor, '_broker_actions_blocked', return_value=False)
        self._blocked_patcher.start()
        self._oco_patcher = patch.dict(os.environ, {'MEIC_15C_OCO_ENABLED': '1'})
        self._oco_patcher.start()

    def tearDown(self):
        self._oco_patcher.stop()
        self._blocked_patcher.stop()

    def test_meic_places_normal_stop_when_oco_flag_disabled(self):
        with patch.dict(os.environ, {'MEIC_15C_OCO_ENABLED': '0'}):
            st = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='10-30',
                side='P',
                short_symbol='.SPXW260820P5400',
                long_symbol='.SPXW260820P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.00,
                long_fill=0.10,
                net_credit=0.90,
                quantity=1,
                open_order_id='open_1',
            )
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, '10-30_P_20260820T103000.json')
                state_mod.save_state(path, st)

                mon = StopMonitor(path, self.broker, prices=self.prices)
                mon.setup_initial_stop(stop_multiplier=2.0)

                active = mon.state.get('active_stop') or {}
                self.assertEqual(active.get('type'), 'STOP_LIMIT')
                self.assertIsNone(active.get('profit_price'))
                self.assertAlmostEqual(active.get('stop_price'), 1.80)
                self.assertAlmostEqual(active.get('limit_price'), 1.90)

    def test_meic_places_oco_initial_stop(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-45',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_1',
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '10-45_P_20260820T104500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)
            mon.setup_initial_stop(stop_multiplier=2.0)

            active = mon.state.get('active_stop') or {}
            self.assertEqual(active.get('type'), 'OCO')
            self.assertAlmostEqual(active.get('profit_price'), 0.15)
            self.assertAlmostEqual(active.get('stop_price'), 1.80)
            self.assertAlmostEqual(active.get('limit_price'), 1.90)
            self.assertEqual(active.get('status'), 'working')
            self.assertEqual(len(self.broker.placed), 1)
            kind, sym, pt, sl, sl_lmt = self.broker.placed[0]
            self.assertEqual(kind, 'oco')
            self.assertEqual(sym, '.SPXW260820P5400')
            self.assertAlmostEqual(pt, 0.15)
            self.assertAlmostEqual(sl, 1.80)
            self.assertAlmostEqual(sl_lmt, 1.90)

    def test_meic_oco_profit_target_fill_handling(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-45',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_1',
        )
        st['active_stop'] = {
            'order_id': 'oco_101',
            'type': 'OCO',
            'profit_price': 0.15,
            'stop_price': 2.00,
            'limit_price': 2.10,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '10-45_P_20260820T104500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)

            # Broker reports profit target filled at 0.15
            fill_result = OrderResult(
                success=True,
                order_id='oco_101',
                status='filled',
                filled_price=0.15,
                filled_quantity=1,
                order_quantity=1,
            )
            mon.handle_stop_order_update(st['active_stop'], broker_result=fill_result)

            self.assertEqual(mon.state.get('status'), 'closing')
            self.assertEqual(mon.state.get('close_mechanism'), 'profit_target_oco')
            self.assertAlmostEqual(mon.state.get('short_close_price'), 0.15)
            self.assertIsNotNone(mon.state.get('short_closed_at'))

    def test_meic_oco_stop_loss_fill_handling(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-45',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_1',
        )
        st['active_stop'] = {
            'order_id': 'oco_102',
            'type': 'OCO',
            'profit_price': 0.15,
            'stop_price': 2.00,
            'limit_price': 2.10,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '10-45_P_20260820T104500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)

            # Broker reports stop loss filled at 2.05
            fill_result = OrderResult(
                success=True,
                order_id='oco_102',
                status='filled',
                filled_price=2.05,
                filled_quantity=1,
                order_quantity=1,
            )
            mon.handle_stop_order_update(st['active_stop'], broker_result=fill_result)

            self.assertEqual(mon.state.get('status'), 'closing')
            self.assertEqual(mon.state.get('close_mechanism'), 'exchange_stop')
            self.assertAlmostEqual(mon.state.get('short_close_price'), 2.05)

    def test_companion_breach_detection_and_breakeven_tightening(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_meic = os.path.join(tmp, 'trades', 'active', 'MEIC_IC')
            os.makedirs(active_meic, exist_ok=True)
            save_meic_settings(MeicSettings(use_oco_15c=True, companion_breach_tightening=True), root=tmp)

            # Put side (open with 2x stop)
            put_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-15',
                side='P',
                short_symbol='.SPXW260820P5400',
                long_symbol='.SPXW260820P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.20,
                long_fill=0.20,
                net_credit=1.00,
                quantity=1,
                open_order_id='open_put',
            )
            put_state['active_stop'] = {
                'order_id': 'put_oco_1',
                'type': 'OCO',
                'profit_price': 0.15,
                'stop_price': 2.40,
                'limit_price': 2.50,
                'phase': 1,
                'status': 'working',
                'quantity': 1,
            }
            put_path = os.path.join(active_meic, '11-15_P_20260820T111500.json')
            state_mod.save_state(put_path, put_state)

            # Call side (breached via software_breach)
            call_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-15',
                side='C',
                short_symbol='.SPXW260820C5600',
                long_symbol='.SPXW260820C5630',
                short_strike=5600,
                long_strike=5630,
                short_fill=1.10,
                long_fill=0.15,
                net_credit=0.95,
                quantity=1,
                open_order_id='open_call',
            )
            call_state['close_mechanism'] = 'software_breach'
            call_state['short_closed_at'] = 1700000000.0
            call_state['status'] = 'closing'
            call_path = os.path.join(active_meic, '11-15_C_20260820T111500.json')
            state_mod.save_state(call_path, call_state)

            # Verify find_companion_trade_state locates Call state for Put state
            companion = find_companion_trade_state(put_state, root=tmp)
            self.assertIsNotNone(companion)
            self.assertEqual(companion.get('lot'), '11-15')
            self.assertTrue(is_companion_breached(companion))

            # Put monitor runs PhaseCompanionBreachAdjustment
            mon = StopMonitor(put_path, self.broker, prices=self.prices)
            phase = PhaseCompanionBreachAdjustment()

            # Mock root for find_companion_trade_state
            import blocks.stop.companion_breach as cb
            orig_root_fn = state_mod._project_root
            state_mod._project_root = lambda: tmp
            try:
                self.assertTrue(phase.should_activate(mon))
                phase.execute(mon)
            finally:
                state_mod._project_root = orig_root_fn

            # Stop on Put side should now be adjusted to breakeven (1.0x / 1.20)
            active = mon.state.get('active_stop') or {}
            self.assertEqual(active.get('type'), 'OCO')
            self.assertAlmostEqual(active.get('profit_price'), 0.15)
            self.assertAlmostEqual(active.get('stop_price'), 1.10)
            self.assertAlmostEqual(active.get('limit_price'), 1.20)
            self.assertAlmostEqual(mon.state.get('stop_multiplier'), 1.0)
            self.assertTrue(mon.state.get('phases', {}).get('breakeven_tightened'))

    def test_companion_breach_skips_when_tightening_disabled(self):
        """When companion_breach_tightening is False (default), PhaseCompanionBreachAdjustment does not activate."""
        with tempfile.TemporaryDirectory() as tmp:
            active_meic = os.path.join(tmp, 'trades', 'active', 'MEIC_IC')
            os.makedirs(active_meic, exist_ok=True)
            save_meic_settings(MeicSettings(companion_breach_tightening=False), root=tmp)

            put_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-15',
                side='P',
                short_symbol='.SPXW260820P5400',
                long_symbol='.SPXW260820P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.20,
                long_fill=0.20,
                net_credit=1.00,
                quantity=1,
                open_order_id='open_put',
            )
            put_state['active_stop'] = {
                'order_id': 'put_stop_1',
                'type': 'STOP_LIMIT',
                'stop_price': 2.40,
                'limit_price': 2.50,
                'phase': 1,
                'status': 'working',
                'quantity': 1,
            }
            put_path = os.path.join(active_meic, '11-15_P_20260820T111500.json')
            state_mod.save_state(put_path, put_state)

            call_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-15',
                side='C',
                short_symbol='.SPXW260820C5600',
                long_symbol='.SPXW260820C5630',
                short_strike=5600,
                long_strike=5630,
                short_fill=1.10,
                long_fill=0.15,
                net_credit=0.95,
                quantity=1,
                open_order_id='open_call',
            )
            call_state['close_mechanism'] = 'software_breach'
            call_state['short_closed_at'] = 1700000000.0
            call_state['status'] = 'closing'
            call_path = os.path.join(active_meic, '11-15_C_20260820T111500.json')
            state_mod.save_state(call_path, call_state)

            mon = StopMonitor(put_path, self.broker, prices=self.prices)
            phase = PhaseCompanionBreachAdjustment()
            orig_root_fn = state_mod._project_root
            state_mod._project_root = lambda: tmp
            try:
                self.assertFalse(phase.should_activate(mon))
            finally:
                state_mod._project_root = orig_root_fn

    def test_companion_profit_target_does_not_tighten_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_meic = os.path.join(tmp, 'trades', 'active', 'MEIC_IC')
            os.makedirs(active_meic, exist_ok=True)

            put_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-45',
                side='P',
                short_symbol='.SPXW260820P5400',
                long_symbol='.SPXW260820P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.20,
                long_fill=0.20,
                net_credit=1.00,
                quantity=1,
                open_order_id='open_put',
            )
            put_path = os.path.join(active_meic, '11-45_P_20260820T114500.json')
            state_mod.save_state(put_path, put_state)

            # Call side closed at profit target (NOT breached)
            call_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='11-45',
                side='C',
                short_symbol='.SPXW260820C5600',
                long_symbol='.SPXW260820C5630',
                short_strike=5600,
                long_strike=5630,
                short_fill=1.10,
                long_fill=0.15,
                net_credit=0.95,
                quantity=1,
                open_order_id='open_call',
            )
            call_state['close_mechanism'] = 'profit_target_oco'
            call_state['short_closed_at'] = 1700000000.0
            call_state['status'] = 'closed'
            call_path = os.path.join(active_meic, '11-45_C_20260820T114500.json')
            state_mod.save_state(call_path, call_state)

            companion = find_companion_trade_state(put_state, root=tmp)
            self.assertIsNotNone(companion)
            self.assertFalse(is_companion_breached(companion))

            mon = StopMonitor(put_path, self.broker, prices=self.prices)
            phase = PhaseCompanionBreachAdjustment()
            orig_root_fn = state_mod._project_root
            state_mod._project_root = lambda: tmp
            try:
                self.assertFalse(phase.should_activate(mon))
            finally:
                state_mod._project_root = orig_root_fn

    def test_companion_search_ignores_past_history_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            active_meic = os.path.join(tmp, 'trades', 'active', 'MEIC_IC')
            history_meic = os.path.join(tmp, 'trades', 'history', 'MEIC_IC')
            os.makedirs(active_meic, exist_ok=True)
            os.makedirs(history_meic, exist_ok=True)

            # Active Call trade on 2026-09-03
            call_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='01-30',
                side='C',
                short_symbol='.SPXW260903C7765',
                long_symbol='.SPXW260903C7795',
                short_strike=7765,
                long_strike=7795,
                short_fill=1.05,
                long_fill=0.10,
                net_credit=0.95,
                quantity=1,
                open_order_id='open_call_1330',
            )
            call_state['entry']['timestamp'] = '2026-09-03T13:29:18-05:00'
            call_path = os.path.join(active_meic, '01-30_C_20260903T132917.json')
            state_mod.save_state(call_path, call_state)

            # Historical stopped-out Put trade from a past day (2026-08-24)
            past_put_state = state_mod.create_new_state(
                strategy='MEIC_IC',
                lot='01-30',
                side='P',
                short_symbol='.SPXW260824P5400',
                long_symbol='.SPXW260824P5370',
                short_strike=5400,
                long_strike=5370,
                short_fill=1.20,
                long_fill=0.20,
                net_credit=1.00,
                quantity=1,
                open_order_id='open_past_put',
            )
            past_put_state['entry']['timestamp'] = '2026-08-24T13:29:03-05:00'
            past_put_state['close_mechanism'] = 'exchange_stop'
            past_put_state['short_closed_at'] = 1787596727.0
            past_put_state['status'] = 'closed'
            past_put_path = os.path.join(history_meic, '01-30_P_20260824T132903.json')
            state_mod.save_state(past_put_path, past_put_state)

            # Must ignore the 2026-08-24 file and return None
            companion = find_companion_trade_state(call_state, root=tmp)
            self.assertIsNone(companion)

    def test_unfilled_companion_is_not_breached(self):
        # A companion trade with status pending_fill / cancelled before fill is NOT breached
        companion = {
            'lot': '01-30',
            'status': 'pending_fill',
            'filled_quantity': 0,
            'short_closed_at': None,
            'close_mechanism': None,
            'short_leg': {'fill_price': 0.0},
        }
        self.assertFalse(is_companion_breached(companion))

        companion['status'] = 'cancelled'
        self.assertFalse(is_companion_breached(companion))

        companion['status'] = 'failed'
        self.assertFalse(is_companion_breached(companion))

    def test_manual_strategy_not_affected_by_companion_breach_or_oco(self):
        from blocks.stop.stop_profile import StopProfile
        manual_profile = StopProfile(
            name='manual_profile',
            spread_type='credit',
            phases=[],
            profit_target_short=None,
            use_oco_short_stop=False,
            adjust_to_breakeven_on_companion_breach=False,
        )
        st = state_mod.create_new_state(
            strategy='MANUAL_SPREAD',
            lot='manual',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_man',
        )
        st['stop_profile'] = 'manual_profile'
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'manual_trade.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices, stop_profile=manual_profile)
            mon.setup_initial_stop(stop_multiplier=2.0)

            active = mon.state.get('active_stop') or {}
            self.assertEqual(active.get('type'), 'STOP_LIMIT')
            self.assertIsNone(active.get('profit_price'))
            kind, sym, sl = self.broker.placed[0]
            self.assertEqual(kind, 'stop')

            phase = PhaseCompanionBreachAdjustment()
            self.assertFalse(phase.should_activate(mon))

    def test_get_order_status_complex_order_sibling_aggregation(self):
        from brokers.tastytrade_broker import TastyTradeBroker
        from unittest.mock import MagicMock
        from decimal import Decimal

        broker = TastyTradeBroker.__new__(TastyTradeBroker)
        broker.get_live_orders_cached = MagicMock(return_value=[])

        # Child 1: cancelled Stop Loss leg
        child1 = MagicMock()
        child1.id = 1001
        child1.complex_order_id = 481599
        child1.status = 'Cancelled'
        child1.legs = []

        # Child 2: filled Profit Target leg @ 0.15
        child2 = MagicMock()
        child2.id = 1002
        child2.complex_order_id = 481599
        child2.status = 'Filled'
        child2.price = Decimal('0.15')
        child2.executed_at = None
        child2.legs = []

        # Child 1 appears first in live_orders list
        live_orders = [child1, child2]

        res = broker.get_order_status('481599', live_orders=live_orders)
        self.assertTrue(res.success)
        self.assertEqual(res.status, 'filled')
        self.assertEqual(res.order_id, '481599')
        self.assertAlmostEqual(res.filled_price, 0.15)

    def test_place_short_stop_skips_when_flat_and_moves_to_closing(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-45',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_1',
        )
        self.broker.single_leg_positions['.SPXW260820P5400'] = 0  # Flat at broker
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '10-45_P_20260820T104500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)
            placed = mon.setup_initial_stop(stop_multiplier=2.0)

            self.assertFalse(placed)
            self.assertEqual(len(self.broker.placed), 0)
            self.assertEqual(mon.state.get('status'), 'closing')
            self.assertEqual(mon.state.get('close_mechanism'), 'broker_position_flat')
            self.assertIsNotNone(mon.state.get('short_closed_at'))

    def test_adjust_stop_to_breakeven_skips_when_flat(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='11-15',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.20,
            long_fill=0.20,
            net_credit=1.00,
            quantity=1,
            open_order_id='open_put',
        )
        st['active_stop'] = {
            'order_id': 'put_oco_1',
            'type': 'OCO',
            'profit_price': 0.15,
            'stop_price': 2.40,
            'limit_price': 2.50,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        self.broker.single_leg_positions['.SPXW260820P5400'] = 0  # Flat
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '11-15_P_20260820T111500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)
            adjusted = mon.adjust_stop_to_breakeven()

            self.assertFalse(adjusted)
            self.assertEqual(mon.state.get('status'), 'closing')
            self.assertEqual(mon.state.get('close_mechanism'), 'broker_position_flat')

    def test_sync_working_stop_order_includes_oco(self):
        st = state_mod.create_new_state(
            strategy='MEIC_IC',
            lot='10-45',
            side='P',
            short_symbol='.SPXW260820P5400',
            long_symbol='.SPXW260820P5370',
            short_strike=5400,
            long_strike=5370,
            short_fill=1.00,
            long_fill=0.10,
            net_credit=0.90,
            quantity=1,
            open_order_id='open_1',
        )
        st['active_stop'] = {
            'order_id': 'oco_sync_1',
            'type': 'OCO',
            'profit_price': 0.15,
            'stop_price': 2.00,
            'limit_price': 2.10,
            'phase': 1,
            'status': 'working',
            'quantity': 1,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, '10-45_P_20260820T104500.json')
            state_mod.save_state(path, st)

            mon = StopMonitor(path, self.broker, prices=self.prices)
            mon._sync_active_close_order = MagicMock()

            mon._sync_working_stop_order()
            mon._sync_active_close_order.assert_called_once()


if __name__ == '__main__':
    unittest.main()
