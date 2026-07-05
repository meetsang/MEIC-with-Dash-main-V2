"""Round-robin StopSupervisor (V3 §6)."""
from __future__ import annotations

import json
import logging
import os
import queue
import signal
import threading
import time
from typing import Dict, List, Optional

from brokers.base import BrokerBase
from blocks.stop import state as state_mod
from blocks.stop.alerts import AlertListener
from blocks.stop.fill_sync import stop_is_current, stop_order_fully_filled
from blocks.stop.monitor import StopMonitor, SLOW_INTERVAL
from blocks.stop.mqtt_prices import MqttPriceCache
from blocks.stop.pending_fill_sync import sync_pending_fills
from blocks.stop.phases import PhaseBase, default_phases
from blocks.stop.v3 import config as v3_config
from blocks.stop.v3.broker_lane import BrokerLane
from blocks.stop.v3.command_claim import (
    apply_killswitch_to_slot,
    check_killswitch_global,
    claim_killswitch,
    detect_and_claim_close_command,
)
from blocks.stop.v3.exit_pool import ExitWorkerPool
from blocks.stop.v3.handlers.exchange_stop_filled import ExchangeStopFilledHandler
from blocks.stop.v3.handlers.long_chase import LongChaseHandler
from blocks.stop.v3.handlers.manual_kill import ManualKillHandler
from blocks.stop.v3.handlers.software_breach import SoftwareBreachHandler
from blocks.stop.v3.recovery import (
    check_exit_stall,
    ensure_v3_exit_fields,
    recover_route,
    reconcile_stalled_exit,
)
from blocks.stop.v3.trade_slot import TradeSlot, merge_disk_state, save_slot
from common.streamer_symbols import register_spread_symbols

log = logging.getLogger(__name__)


class StopSupervisor:
    """Single-thread scan loop + exit worker pool (V3)."""

    def __init__(
        self,
        broker: BrokerBase,
        prices: MqttPriceCache,
        alert_listener: Optional[AlertListener] = None,
        poll_interval: float = 5.0,
        phases: Optional[List[PhaseBase]] = None,
    ):
        self.broker = broker
        self.prices = prices
        self.alert_listener = alert_listener
        self.poll_interval = poll_interval
        self.phases = sorted(phases or default_phases(), key=lambda p: p.priority)
        self.lane = BrokerLane()
        self.exit_pool = ExitWorkerPool()
        self._slots: Dict[str, TradeSlot] = {}
        self._stop = threading.Event()
        self._killswitch_claimed = False
        self._loop_count = 0
        self._alert_order_paths: Dict[str, str] = {}

    def run_forever(self) -> None:
        self.prices.start()
        if self.alert_listener:
            self.alert_listener.start()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        log.info(
            'StopSupervisor V3 watching %s (TARGET_CYCLE_SEC=%.2fs)',
            ', '.join(state_mod.all_active_dirs()),
            v3_config.TARGET_CYCLE_SEC,
        )

        while not self._stop.is_set():
            t0 = time.monotonic()
            self._loop_count += 1
            try:
                self._cycle()
            except Exception:
                log.exception('Supervisor cycle failed')
            elapsed = time.monotonic() - t0
            sleep_for = max(0.0, v3_config.TARGET_CYCLE_SEC - elapsed)
            if sleep_for:
                self._stop.wait(sleep_for)
            else:
                time.sleep(0)

        self.shutdown()

    def _signal_handler(self, signum, frame) -> None:
        log.info('Signal %s — shutting down StopSupervisor', signum)
        self._stop.set()

    def shutdown(self) -> None:
        self.prices.stop()
        if self.alert_listener:
            self.alert_listener.stop()
        log.info('StopSupervisor stopped')

    def _write_heartbeat(self, active_trades: int) -> None:
        try:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
            hb_path = os.path.join(root, 'trades', 'heartbeat.json')
            os.makedirs(os.path.dirname(hb_path), exist_ok=True)
            with open(hb_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'ts': state_mod.now_iso(),
                    'engine': 'v3',
                    'loop_count': self._loop_count,
                    'active_trades': active_trades,
                    'active_slots': active_trades,
                    'active_exit_jobs': len(self.exit_pool.active_paths),
                    'exit_jobs': len(self.exit_pool.active_paths),
                    'broker_in_flight': self.lane.in_flight,
                    'broker_lane_max': self.lane.max_concurrent,
                    'target_cycle_sec': v3_config.TARGET_CYCLE_SEC,
                }, f)
        except Exception:
            pass

    def _sync_pending_fills(self) -> None:
        try:
            sync_pending_fills(self.broker)
        except Exception:
            log.exception('Pending fill sync failed')

    def _discover_slots(self) -> List[TradeSlot]:
        self._sync_pending_fills()
        paths = list(state_mod.iter_active_trade_paths())
        slots: List[TradeSlot] = []
        seen = set()

        for path in paths:
            seen.add(path)
            try:
                st = state_mod.load_state(path)
            except Exception as exc:
                log.warning('Skip slot %s — load failed: %s', path, exc)
                continue

            status = st.get('status')
            if status not in ('open', 'closing'):
                if path in self._slots:
                    del self._slots[path]
                continue

            filled = int(st.get('filled_quantity') or 0)
            target = int(st.get('quantity') or 0)
            if status == 'open' and target and filled < target:
                continue

            if path in self._slots:
                slot = self._slots[path]
                merge_disk_state(slot)
                self._sync_alert_registration(slot)
            else:
                slot = TradeSlot.from_path(path)
                ensure_v3_exit_fields(slot.state)
                lot = slot.state.get('lot', 'stop-monitor')
                register_spread_symbols(slot.state, lot, log)
                route = recover_route(slot)
                if route:
                    log.info('V3 startup recovery path=%s route=%s', path, route)
                self._sync_alert_registration(slot)
                self._slots[path] = slot
            slots.append(slot)

        for path in list(self._slots.keys()):
            if path not in seen:
                del self._slots[path]

        return slots

    def _sync_alert_registration(self, slot: TradeSlot) -> None:
        """Keep AlertListener order_id → path map current (§6.5)."""
        if not self.alert_listener:
            return
        active = slot.state.get('active_stop') or {}
        oid = active.get('order_id')
        if not oid:
            return
        oid = str(oid)
        prev_path = self._alert_order_paths.get(oid)
        if prev_path == slot.path:
            return
        if prev_path and prev_path != slot.path:
            self.alert_listener.unregister(oid)
        try:
            fill_q = self.alert_listener.register(oid)
            self._alert_order_paths[oid] = slot.path
            if slot.legacy_monitor is not None:
                slot.legacy_monitor.fill_queue = fill_q
        except Exception as exc:
            log.warning('Alert registration failed for %s: %s', oid, exc)

    def _legacy_monitor(self, slot: TradeSlot) -> StopMonitor:
        if slot.legacy_monitor is None:
            fill_q: Optional[queue.Queue] = None
            active = slot.state.get('active_stop') or {}
            oid = active.get('order_id')
            if oid and self.alert_listener:
                try:
                    fill_q = self.alert_listener.register(str(oid))
                except Exception:
                    pass
            mon = StopMonitor(
                slot.path,
                self.broker,
                self.prices,
                phases=self.phases,
                fill_queue=fill_q,
                alert_listener=self.alert_listener,
            )
            mon._skip_dashboard_commands = True
            slot.legacy_monitor = mon
        slot.legacy_monitor.state = slot.state
        return slot.legacy_monitor

    def _enqueue_manual_kill(self, slot: TradeSlot, *, reason: str) -> None:
        slot.exit_job_id = 'pending'

        def _job() -> None:
            try:
                ManualKillHandler(
                    slot, self.broker, self.prices, self.lane, self.alert_listener,
                ).run(reason=reason)
            finally:
                slot.exit_job_id = None

        if not self.exit_pool.submit_manual_kill(slot, _job):
            slot.exit_job_id = None

    def _enqueue_exchange_stop_filled(
        self,
        slot: TradeSlot,
        *,
        stop: dict,
        broker_result=None,
    ) -> None:
        if self.exit_pool.has_job(slot.path):
            log.info('exit_duplicate_ignored path=%s event=exchange_stop_filled', slot.path)
            return
        slot.exit_job_id = 'pending'

        def _job() -> None:
            try:
                ExchangeStopFilledHandler(
                    slot, self.broker, self.prices, self.lane, self.alert_listener,
                ).run(stop=stop, broker_result=broker_result)
            finally:
                slot.exit_job_id = None

        if not self.exit_pool.submit(slot, _job, job_kind='exchange_stop_filled'):
            slot.exit_job_id = None

    def _enqueue_software_breach(self, slot: TradeSlot, phase: PhaseBase) -> None:
        if self.exit_pool.has_job(slot.path):
            return
        slot.exit_job_id = 'pending'

        def _job() -> None:
            try:
                SoftwareBreachHandler(
                    slot,
                    self.broker,
                    self.prices,
                    self.lane,
                    phase,
                    self.alert_listener,
                ).run()
            finally:
                slot.exit_job_id = None

        if not self.exit_pool.submit(slot, _job, job_kind=f'breach_{phase.name}'):
            slot.exit_job_id = None

    def _enqueue_long_chase(self, slot: TradeSlot) -> None:
        if self.exit_pool.has_job(slot.path):
            return
        slot.exit_job_id = 'pending'

        def _job() -> None:
            try:
                LongChaseHandler(
                    slot, self.broker, self.prices, self.lane, self.alert_listener,
                ).run()
            finally:
                slot.exit_job_id = None

        if self.exit_pool.submit(slot, _job, job_kind='long_chase'):
            slot.long_chase_scheduled_at = time.time()

    def _drain_alert_fills(self, slot: TradeSlot) -> bool:
        """Returns True if an exchange-stop handler was enqueued."""
        mon = self._legacy_monitor(slot)
        fq = mon.fill_queue
        if fq is None:
            return False
        handled = False
        while not fq.empty():
            try:
                event = fq.get_nowait()
            except queue.Empty:
                break
            order_id = str(event.get('order_id', ''))
            active = slot.state.get('active_stop') or {}
            if str(active.get('order_id', '')) != order_id:
                continue
            status = str(event.get('status', 'filled')).lower()
            active['status'] = status
            if status != 'filled':
                continue
            result = self.broker.get_order_status(order_id)
            if stop_order_fully_filled(slot.state, result):
                self._enqueue_exchange_stop_filled(
                    slot, stop=active, broker_result=result,
                )
                handled = True
        slot.state = mon.state
        return handled

    def _slow_broker_sync(self, slot: TradeSlot) -> bool:
        """REST reconcile — may detect stop fill. Returns True if C2 enqueued."""
        mon = self._legacy_monitor(slot)
        mon._reconcile_active_stop_with_broker()
        slot.state = mon.state
        if slot.state.get('status') == 'closing' and slot.state.get('short_closed_at'):
            return True
        active = slot.state.get('active_stop') or {}
        if active.get('status') == 'filled':
            self._enqueue_exchange_stop_filled(slot, stop=active)
            return True
        return False

    def _scan_open_slot(self, slot: TradeSlot) -> None:
        mon = self._legacy_monitor(slot)

        if mon._broker_actions_frozen():
            save_slot(slot)
            return

        if self._drain_alert_fills(slot):
            return

        if self.exit_pool.has_job(slot.path):
            slot.exit_job_id = 'active'
            return

        streamer_stale = mon._streamer_prices_stale()
        mon._refresh_breach_watch(streamer_stale)

        now = time.time()
        if now - slot.last_broker_sync >= SLOW_INTERVAL:
            slot.last_broker_sync = now
            if self._slow_broker_sync(slot):
                save_slot(slot)
                return

        if streamer_stale:
            save_slot(slot)
            return

        mon.kill_switch = self.prices.kill_switch

        for phase in self.phases:
            if phase.should_activate(mon):
                self._enqueue_software_breach(slot, phase)
                save_slot(slot)
                return

        if not stop_is_current(slot.state):
            mon._ensure_stop_for_filled_qty()
            slot.state = mon.state

        save_slot(slot)

    def _poll_closing(self, slot: TradeSlot) -> None:
        mon = self._legacy_monitor(slot)

        if slot.state.get('spread_close_order_id'):
            mon._poll_spread_close()
            slot.state = mon.state
            save_slot(slot)
            return

        close_started = slot.state.get('short_closed_at')
        if close_started is None:
            return

        delay = mon.long_close_delay_sec
        if time.time() - float(close_started) < delay:
            return

        self._enqueue_long_chase(slot)

    def _scan_slot(self, slot: TradeSlot, *, killswitch_active: bool) -> None:
        merge_disk_state(slot)
        ensure_v3_exit_fields(slot.state)

        if check_exit_stall(slot):
            log.critical('Exit stalled on %s — operator review', slot.path)
            if not self.exit_pool.has_job(slot.path):
                reconcile_stalled_exit(slot, self.broker)
            save_slot(slot)

        if killswitch_active and slot.status == 'open' and not slot.close_only_mode:
            if apply_killswitch_to_slot(slot):
                self._enqueue_manual_kill(slot, reason='admin_killswitch')
                return

        claimed, mechanism = detect_and_claim_close_command(slot)
        if claimed:
            self._enqueue_manual_kill(slot, reason=mechanism)
            return

        if self.exit_pool.has_job(slot.path):
            slot.exit_job_id = 'active'
            return

        manual_handlers = ('manual_close', 'admin_killswitch')
        if slot.close_only_mode or slot.state.get('exit_handler') in manual_handlers:
            if slot.status == 'closing':
                self._poll_closing(slot)
            return

        if slot.status == 'closing':
            self._poll_closing(slot)
            return

        if slot.status == 'open':
            self._scan_open_slot(slot)

    def _cycle(self) -> None:
        slots = self._discover_slots()
        killswitch = check_killswitch_global()
        if killswitch and not self._killswitch_claimed:
            self._killswitch_claimed = claim_killswitch()
        if not killswitch:
            self._killswitch_claimed = False

        for slot in slots:
            self._scan_slot(slot, killswitch_active=killswitch)

        self._write_heartbeat(len(slots))
