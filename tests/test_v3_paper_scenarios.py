"""V3 paper scenarios — design doc §12.5 (offline, no live C1/C2)."""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from blocks.stop.monitor import StopMonitor
from blocks.stop.phases import PhaseBase
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.command_claim import detect_and_claim_close_command
from blocks.stop.v3.exit_pool import ExitWorkerPool
from blocks.stop.v3.handlers.manual_kill import ManualKillHandler
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import TradeSlot, merge_disk_state, merge_policy
from scripts.seed_dual_manual_kill_fixture import FIXTURES, _build_state, _filename
from tests.mock_broker import MockBroker


def _open_state(**overrides):
    st = state_mod.create_new_state(
        strategy='MANUAL_SPREAD',
        lot='ms-v3',
        side='C',
        short_symbol='.SPXW260706C7600',
        long_symbol='.SPXW260706C7625',
        short_strike=7600,
        long_strike=7625,
        short_fill=0.82,
        long_fill=0.27,
        net_credit=0.55,
        quantity=3,
        open_order_id='open-v3',
    )
    st['active_stop'] = {
        'order_id': '9001',
        'type': 'STOP_LIMIT',
        'stop_price': 1.7,
        'limit_price': 1.8,
        'phase': 1,
        'status': 'working',
        'quantity': 3,
    }
    st['stop_quantity'] = 3
    st['recovery'] = {}
    st.update(overrides)
    return st


def _mock_prices():
    prices = MagicMock()
    prices.get_spx.return_value = 7483.0
    prices.get_market_mid = MagicMock(return_value=0.20)
    prices.get = prices.get_market_mid
    prices.get_quote = MagicMock(return_value=None)
    prices.current_stream_session_id = MagicMock(return_value=None)
    prices.last_event_kind = MagicMock(return_value=None)
    prices.kill_switch = False
    prices.is_stale.return_value = False
    prices.start = MagicMock()
    prices.stop = MagicMock()
    return prices


class TestV2RollbackCloseOnly(unittest.TestCase):
    """§12.5 #20 — V2 honors close_only_mode; no breach scan."""

    def test_poll_once_skips_breach_when_close_only(self):
        broker = MockBroker()
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='closing',
                close_only_mode=True,
                exit_handler='manual_close',
                spread_close_order_id='8800',
            )
            state_mod.save_state(path, st)
            broker.orders['8800'] = OrderResult(True, '8800', 'working')

            phase = MagicMock(spec=PhaseBase)
            phase.should_activate = MagicMock(return_value=True)
            phase.name = 'phase2'

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
                mon = StopMonitor(path, broker, prices, phases=[phase])
                mon._poll_once()

            phase.should_activate.assert_not_called()
            self.assertTrue(mon.state.get('close_only_mode'))


class TestRestartMidClose(unittest.TestCase):
    """§12.5 #16 — supervisor resumes closing handler after restart."""

    def test_supervisor_resumes_manual_kill_on_open_close_only_restart(self):
        """Fix F-1: open + close_only_mode must re-enqueue ManualKillHandler."""
        broker = MockBroker()
        broker.orders['9001'] = OrderResult(True, '9001', 'working')
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
            self.assertEqual(slot.state.get('status'), 'closed')
            self.assertFalse(slot.state.get('close_only_mode'))
            self.assertIsNotNone(slot.state.get('exit_audit'))

    def test_supervisor_polls_spread_close_on_restart(self):
        broker = MockBroker()
        broker.orders['8800'] = OrderResult(
            True, '8800', 'filled',
            filled_price=0.25,
            filled_quantity=3,
            short_fill_price=0.30,
            long_fill_price=0.05,
        )
        prices = _mock_prices()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='closing',
                close_only_mode=True,
                exit_handler='manual_close',
                exit_started_at=state_mod.now_iso(),
                spread_close_order_id='8800',
                close_mechanism='manual_close',
            )
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'):
                sup._cycle()
                time.sleep(0.5)

            self.assertEqual(slot.state.get('status'), 'closed')

    def test_supervisor_resumes_long_chase_after_exchange_stop(self):
        broker = MockBroker()
        broker.prices['.SPXW260706C7625'] = 0.08
        prices = _mock_prices()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(
                status='closing',
                close_mechanism='exchange_stop',
                exit_handler='exchange_stop',
                exit_started_at=state_mod.now_iso(),
                short_closed_at=time.time() - 60,
            )
            st['active_stop']['status'] = 'filled'
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup, '_discover_slots', return_value=[slot]), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'):
                sup._cycle()
                time.sleep(1.0)

            self.assertIsNotNone(slot.state.get('long_close_order_id'))


class TestManualJsonEdit(unittest.TestCase):
    """§12.5 #17 — disk edit visible after mtime-gated merge."""

    def test_merge_picks_up_exit_fields_from_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            mem = _open_state()
            disk = dict(mem)
            disk['close_only_mode'] = True
            disk['exit_handler'] = 'manual_close'
            disk['exit_started_at'] = state_mod.now_iso()
            state_mod.save_state(path, disk)

            slot = TradeSlot.from_path(path)
            slot.state = mem
            slot.disk_mtime = 0.0
            merge_disk_state(slot)

            self.assertTrue(slot.state.get('close_only_mode'))
            self.assertEqual(slot.state.get('exit_handler'), 'manual_close')

    def test_merge_policy_preserves_v3_exit_fields(self):
        mem = {'lot': 'a', 'close_only_mode': False}
        disk = {'lot': 'b', 'close_only_mode': True, 'exit_handler': 'manual_close'}
        merged = merge_policy(mem, disk)
        self.assertTrue(merged['close_only_mode'])
        self.assertEqual(merged['exit_handler'], 'manual_close')

    def test_merge_policy_adopts_dashboard_stop_multiplier(self):
        mem = {
            'stop_multiplier': 2.0,
            'plan': {'stop_multiplier': 2.0},
            'entry': {'two_x_net_credit': 0.6},
            'short_leg': {'two_x_short': 0.9},
        }
        disk = {
            'stop_multiplier': 3.0,
            'plan': {'stop_multiplier': 3.0},
            'entry': {'two_x_net_credit': 0.9},
            'short_leg': {'two_x_short': 1.35},
        }
        merged = merge_policy(mem, disk)
        self.assertEqual(merged['stop_multiplier'], 3.0)
        self.assertEqual(merged['plan']['stop_multiplier'], 3.0)
        self.assertEqual(merged['entry']['two_x_net_credit'], 0.9)
        self.assertEqual(merged['short_leg']['two_x_short'], 1.35)


class TestStopFilledDuringManualKill(unittest.TestCase):
    """§12.5 #19 — kill cancel sees fill → C2 path, no spread close."""

    def test_stop_filled_during_cancel_skips_spread_close(self):
        broker = MockBroker()
        broker.orders['9001'] = OrderResult(
            True, '9001', 'filled',
            filled_price=1.75,
            filled_quantity=3,
            order_quantity=3,
        )
        prices = _mock_prices()

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            state_mod.save_state(path, _open_state())
            slot = TradeSlot.from_path(path)
            lane = BrokerLane(max_concurrent=4)

            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(StopMonitor, '_cancel_stop_and_confirm', return_value='filled'):
                ManualKillHandler(slot, broker, prices, lane).run(reason='manual_close')

            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertEqual(len(spread_closes), 0)
            self.assertEqual(slot.state.get('status'), 'closing')
            self.assertIsNotNone(slot.state.get('short_closed_at'))


class TestExitIdempotency(unittest.TestCase):
    """§12.5 #18 — one exit job per trade path."""

    def test_duplicate_exit_job_rejected(self):
        pool = ExitWorkerPool(max_jobs=4)
        slot = TradeSlot(path='/tmp/a.json', state={})
        started = threading.Event()
        gate = threading.Event()

        def _slow() -> None:
            started.set()
            gate.wait(timeout=5)

        self.assertTrue(pool.submit(slot, _slow, job_kind='test'))
        self.assertFalse(pool.submit(slot, _slow, job_kind='test'))
        started.wait(timeout=2)
        gate.set()
        time.sleep(0.2)
        self.assertFalse(pool.has_job(slot.path))

    def test_close_only_blocks_second_kill_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd_dir = os.path.join(tmp, 'commands')
            os.makedirs(cmd_dir, exist_ok=True)
            path = os.path.join(tmp, 't.json')
            st = _open_state(close_only_mode=True, exit_handler='manual_close')
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            cmd_path = os.path.join(cmd_dir, 't.json.close.json')
            with open(cmd_path, 'w', encoding='utf-8') as f:
                f.write('{"close_mechanism":"manual_close"}')

            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp):
                claimed, _ = detect_and_claim_close_command(slot)

            self.assertFalse(claimed)
            self.assertFalse(os.path.exists(cmd_path))


class TestV3SupervisorDualKill(unittest.TestCase):
    """§12.5 #14 — four-path parallelism via supervisor (2-trade fixture)."""

    def test_supervisor_dual_kill_from_commands(self):
        broker = MockBroker()
        prices = _mock_prices()
        paths = []

        with tempfile.TemporaryDirectory() as tmp:
            cmd_dir = os.path.join(tmp, 'commands')
            os.makedirs(cmd_dir, exist_ok=True)

            for spec, lot in zip(FIXTURES, ('ms-99', 'ms-100')):
                state = _build_state(spec, lot=lot)
                fname = _filename(lot)
                path = os.path.join(tmp, fname)
                state_mod.save_state(path, state)
                broker.orders[spec['stop_order_id']] = OrderResult(
                    True, spec['stop_order_id'], 'working',
                )
                paths.append(path)
                cmd_path = os.path.join(cmd_dir, f'{fname}.close.json')
                with open(cmd_path, 'w', encoding='utf-8') as f:
                    f.write('{"close_mechanism":"manual_close"}')

            sup = StopSupervisor(broker, prices)
            slots = [TradeSlot.from_path(p) for p in paths]

            t0 = time.monotonic()
            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch('common.streamer_symbols.register_spread_symbols'), \
                 patch.object(sup, '_discover_slots', return_value=slots), \
                 patch.object(sup, '_sync_pending_fills'), \
                 patch.object(sup, '_write_heartbeat'):
                sup._cycle()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if not sup.exit_pool.active_paths:
                        break
                    time.sleep(0.05)
            wall = time.monotonic() - t0

            spread_closes = [p for p in broker.placed if p[0] == 'spread_close']
            self.assertEqual(len(spread_closes), 2)
            for slot in slots:
                self.assertEqual(slot.state.get('status'), 'closed')
                self.assertFalse(slot.state.get('close_only_mode'))
            self.assertLess(wall, 8.0, f'dual supervisor kill wall={wall:.2f}s')
