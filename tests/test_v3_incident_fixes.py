"""July 6 incident regression — F-3/F-4/F-5/F-8/F-9."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from blocks.stop import state as state_mod
from blocks.stop.phases import PhaseAction, Phase1InitialStop
from blocks.stop.v3.recovery import resolve_exit_recovery_route
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import TradeSlot
from tests.mock_broker import MockBroker
from tests.test_v3_paper_scenarios import _mock_prices, _open_state


class TestV3IncidentFixes(unittest.TestCase):
    def test_open_trade_does_not_enqueue_breach_handler(self):
        broker = MockBroker()
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state()
            st['breach_watch'] = {
                'status': 'ok',
                'spread_mid': 0.5,
                'threshold': 1.7,
                'short_mqtt': True,
                'long_mqtt': True,
            }
            st['lifecycle'] = {'breach_arm_status': 'armed'}
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'), \
                 patch.object(sup, '_slow_broker_sync', return_value=False), \
                 patch.object(sup, '_enqueue_confirmed_exit') as mock_exit:
                sup._cycle()

            mock_exit.assert_not_called()

    def test_supervisor_does_not_manual_kill_on_breach_exit_handler(self):
        broker = MockBroker()
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='open',
                close_only_mode=True,
                exit_handler='breach_phase1_initial_stop',
                exit_started_at=state_mod.now_iso(),
                close_mechanism='software_breach',
            )
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            route = resolve_exit_recovery_route(slot)
            self.assertNotEqual(route, 'resume_manual_kill')

            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'), \
                 patch.object(sup, '_slow_broker_sync', return_value=False), \
                 patch.object(sup, '_enqueue_manual_kill') as mock_mk:
                sup._cycle()

            mock_mk.assert_not_called()
            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertEqual(spread_closes, [])

    def test_manual_kill_still_resumes_for_manual_close(self):
        broker = MockBroker()
        broker.orders['9001'] = __import__('brokers.base', fromlist=['OrderResult']).OrderResult(
            True, '9001', 'working',
        )
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='open',
                close_only_mode=True,
                exit_handler='manual_close',
                exit_started_at=state_mod.now_iso(),
                close_mechanism='manual_close',
            )
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            route = resolve_exit_recovery_route(slot)
            self.assertEqual(route, 'resume_manual_kill')

            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'):
                sup._cycle()
                time.sleep(1.0)

            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertGreaterEqual(len(spread_closes), 1)

    def test_phase1_evaluate_open_trade_is_not_exit_required(self):
        broker = MockBroker()
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            state_mod.save_state(path, _open_state())
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                from blocks.stop.monitor import StopMonitor
                mon = StopMonitor(path, broker, prices, phases=[Phase1InitialStop()])
            phase = Phase1InitialStop()
            self.assertEqual(phase.evaluate(mon), PhaseAction.NONE)

    def test_broker_blocks_spread_close_when_flat(self):
        broker = MockBroker()
        broker.spread_position_flat = True
        result = broker.place_spread_close_order('S', 'L', 3, 1.0)
        self.assertFalse(result.success)
        self.assertEqual(result.status, 'rejected_preflight')
        self.assertFalse(result.transmitted)

    def test_long_chase_waits_30s_from_fill_time(self):
        broker = MockBroker()
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            fill_ts = time.time() - 9.0
            st = _open_state(
                status='closing',
                close_mechanism='exchange_stop',
                short_closed_at=fill_ts,
            )
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)
            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_enqueue_long_chase') as mock_lc:
                sup._poll_closing(slot)
            mock_lc.assert_not_called()

            st['short_closed_at'] = time.time() - 31.0
            slot.state = st
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_enqueue_long_chase') as mock_lc2:
                sup._poll_closing(slot)
            mock_lc2.assert_called_once()

    def test_tasty_spread_close_blocks_unknown_position_by_default(self):
        from brokers.tastytrade_broker import TastyTradeBroker

        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock()
        with patch.object(broker, 'inspect_spread_position', return_value='unknown'):
            result = broker.place_spread_close_order('S', 'L', 3, 1.0)
        self.assertFalse(result.success)
        self.assertEqual(result.status, 'rejected_preflight')
        self.assertFalse(result.transmitted)
        broker._run.assert_not_called()

    def test_tasty_spread_close_allows_unknown_only_with_emergency_override(self):
        from brokers.base import OrderResult
        from brokers.tastytrade_broker import TastyTradeBroker

        broker = object.__new__(TastyTradeBroker)
        broker._run = MagicMock(return_value=MagicMock())
        with patch.object(broker, 'inspect_spread_position', return_value='unknown'), \
             patch('brokers.tastytrade_broker._order_result_from_response',
                   return_value=OrderResult(True, '1', 'working')):
            result = broker.place_spread_close_order(
                'S', 'L', 3, 1.0, allow_unverified_emergency_close=True,
            )
        self.assertTrue(result.success)
        broker._run.assert_called_once()

    def test_manual_kill_blocks_unknown_position_without_transmission(self):
        from blocks.stop.v3.handlers.manual_kill import ManualKillHandler

        broker = MockBroker()
        broker.spread_position_state = 'unknown'
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='open',
                close_only_mode=True,
                exit_handler='manual_close',
                exit_started_at=state_mod.now_iso(),
            )
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)
            handler = ManualKillHandler(slot, broker, prices, MagicMock())
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(handler, 'lane') as lane:
                lane.run = lambda _tid, fn: fn()
                handler.run(reason='manual_close')
        spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
        self.assertEqual(spread_closes, [])
        self.assertEqual(slot.state.get('exit_error'), 'preflight_unknown')

    def test_phase3_proximity_exit_not_blocked_by_phase1_breach_watch_no_prices(self):
        import datetime as dt

        broker = MockBroker()
        prices = _mock_prices()
        prices.get.return_value = None
        prices.get_spx.return_value = 7598.0

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state()
            st['breach_watch'] = {'status': 'no_prices'}
            st['lifecycle'] = {'breach_arm_status': 'waiting_mqtt'}
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            sup = StopSupervisor(broker, prices)
            fake_now = dt.time(14, 55, 0)

            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor.streamer_prices_stale', return_value=False), \
                 patch('meic0dte.app.utilities.central_time', return_value=fake_now), \
                 patch('blocks.stop.monitor.central_time', return_value=fake_now), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'), \
                 patch.object(sup, '_slow_broker_sync', return_value=False), \
                 patch.object(sup, '_enqueue_confirmed_exit') as mock_exit:
                sup._cycle()

            self.assertEqual(mock_exit.call_count, 1)
            self.assertEqual(mock_exit.call_args[0][1].name, 'phase3_spx_proximity')
