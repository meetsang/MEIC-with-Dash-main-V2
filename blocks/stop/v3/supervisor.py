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
from blocks.stop.stop_ownership import (
    apply_ownership_conflict_flags,
    has_ownership_conflict,
    scan_duplicate_stop_ownership,
)
from blocks.stop.monitor import StopMonitor, SLOW_INTERVAL
from blocks.stop.mqtt_prices import MqttPriceCache
from common.mqtt_prices import mqtt_cache_is_stale, write_mqtt_cache_health
from blocks.stop.pending_fill_sync import sync_pending_fills
from blocks.stop.phases import PhaseAction, PhaseBase, default_phases
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
    resolve_exit_recovery_route,
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
        self._last_pending_sync = 0.0
        self._ownership_conflict_paths: set[str] = set()

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
            payload = {
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
            }
            tmp = f'{hb_path}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
                f.flush()
            os.replace(tmp, hb_path)
            write_mqtt_cache_health(self.prices, root=root)
        except Exception:
            pass

    def _sync_pending_fills(self) -> None:
        now = time.time()
        if now - self._last_pending_sync < SLOW_INTERVAL:
            return
        self._last_pending_sync = now
        try:
            sync_pending_fills(self.broker)
        except Exception:
            log.exception('Pending fill sync failed')

    @staticmethod
    def _slot_eligible(st: dict) -> bool:
        status = st.get('status')
        if status not in ('open', 'closing'):
            return False
        filled = int(st.get('filled_quantity') or 0)
        target = int(st.get('quantity') or 0)
        if status == 'open' and target and filled < target:
            return False
        return True

    def _discover_slots(self) -> List[TradeSlot]:
        self._sync_pending_fills()
        paths = list(state_mod.iter_active_trade_paths())
        slots: List[TradeSlot] = []
        seen = set()

        for path in paths:
            seen.add(path)

            if path in self._slots:
                slot = self._slots[path]
                merge_disk_state(slot)
                self._sync_alert_registration(slot)
                if not self._slot_eligible(slot.state):
                    del self._slots[path]
                    continue
                slots.append(slot)
                continue

            try:
                st = state_mod.load_state(path)
            except Exception as exc:
                log.warning('Skip slot %s — load failed: %s', path, exc)
                continue

            if not self._slot_eligible(st):
                continue

            slot = TradeSlot.from_loaded(path, st)
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

    def _enqueue_confirmed_exit(self, slot: TradeSlot, phase: PhaseBase) -> None:
        self._enqueue_software_breach(slot, phase)

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

    def _lifecycle_section(self, slot: TradeSlot) -> dict:
        lc = slot.state.setdefault('lifecycle', {})
        if not isinstance(lc, dict):
            lc = {}
            slot.state['lifecycle'] = lc
        return lc

    def _breach_arm_ready(self, slot: TradeSlot, mon: StopMonitor) -> tuple[bool, str]:
        """F-8 gates before phase breach evaluation."""
        st = slot.state
        if st.get('close_only_mode'):
            return False, 'exit_in_progress'
        if str(st.get('status') or '') != 'open':
            return False, 'not_open'
        filled = int(st.get('filled_quantity') or 0)
        if filled <= 0:
            return False, 'not_filled'
        short_fill = float((st.get('short_leg') or {}).get('fill_price') or 0)
        long_fill = float((st.get('long_leg') or {}).get('fill_price') or 0)
        if short_fill <= 0 or long_fill <= 0:
            return False, 'legs_not_filled'

        active = st.get('active_stop') or {}
        if not active.get('order_id'):
            return False, 'waiting_stop'
        stop_qty = int(st.get('stop_quantity') or 0)
        if stop_qty < filled:
            return False, 'waiting_stop'

        stop_st = str(active.get('status') or '').lower()
        if stop_st in ('cancelled', 'canceled', 'rejected', ''):
            return False, 'waiting_stop'

        watch = st.get('breach_watch') or {}
        wstatus = str(watch.get('status') or '')
        if mqtt_cache_is_stale(mon.prices):
            return False, 'stale_mqtt'
        if wstatus == 'stale':
            return False, 'stale_mqtt' if watch.get('mqtt_cache_stale') else 'waiting_mqtt'
        if wstatus == 'no_prices':
            return False, 'waiting_mqtt'

        short_sym = st['short_leg']['symbol']
        long_sym = st['long_leg']['symbol']
        if mon.prices.get_market_mid(short_sym) is None or mon.prices.get_market_mid(long_sym) is None:
            return False, 'waiting_mqtt'

        return True, 'armed'

    def _stop_is_current_for_slot(self, slot: TradeSlot) -> bool:
        return stop_is_current(
            slot.state,
            ownership_conflict=(
                slot.path in self._ownership_conflict_paths
                or has_ownership_conflict(slot.state)
            ),
        )

    def _phase3_scan_ready(self, slot: TradeSlot, mon: StopMonitor) -> bool:
        """Phase 3 uses SPX + time only — not option-leg MQTT breach arm."""
        st = slot.state
        if st.get('close_only_mode'):
            return False
        if str(st.get('status') or '') != 'open':
            return False
        if int(st.get('filled_quantity') or 0) <= 0:
            return False
        if mqtt_cache_is_stale(mon.prices):
            return False
        if mon.prices.get_spx() is None:
            return False
        return self._stop_is_current_for_slot(slot)

    def _maybe_enqueue_phase3_exit(self, slot: TradeSlot, mon: StopMonitor) -> bool:
        phase3 = next((p for p in self.phases if p.name == 'phase3_spx_proximity'), None)
        if phase3 is None:
            return False
        if not self._phase3_scan_ready(slot, mon):
            return False
        if not phase3.should_activate(mon):
            return False
        action = phase3.evaluate(mon)
        if action != PhaseAction.EXIT_REQUIRED:
            return False
        log.info(
            'Confirmed exit required phase=%s path=%s (SPX proximity)',
            phase3.name,
            slot.path,
        )
        self._enqueue_confirmed_exit(slot, phase3)
        return True

    def _mark_breach_armed(self, slot: TradeSlot, mon: StopMonitor) -> None:
        lc = self._lifecycle_section(slot)
        if lc.get('breach_armed_at'):
            return
        lc['breach_armed_at'] = state_mod.now_iso()
        lc['breach_arm_status'] = 'armed'
        active = slot.state.get('active_stop') or {}
        watch = slot.state.get('breach_watch') or {}
        log.info(
            'Breach armed %s %s: stop=%s spread_mid=%s threshold=%s',
            slot.state.get('lot', '?'),
            (slot.state.get('entry') or {}).get('side', '?'),
            active.get('order_id'),
            watch.get('spread_mid'),
            watch.get('threshold'),
        )

    def _apply_recovery_route(self, slot: TradeSlot, route: str) -> bool:
        """Returns True if route handled (no further scan this cycle)."""
        if route == 'none':
            return False
        if route == 'quarantine':
            log.error(
                'Recover route unknown close_only state — quarantine path=%s handler=%s',
                slot.path,
                slot.state.get('exit_handler'),
            )
            return True
        if route == 'resume_manual_kill':
            reason = (
                slot.state.get('close_mechanism')
                or slot.state.get('exit_handler')
                or 'manual_close'
            )
            log.info(
                'Recover route manual_close → ManualKillHandler path=%s reason=%s',
                slot.path,
                reason,
            )
            self._enqueue_manual_kill(slot, reason=str(reason))
            return True
        if route in ('poll_close_order', 'resume_long_chase'):
            log.info('Recover route %s path=%s', route, slot.path)
            self._poll_closing(slot)
            return True
        if route == 'resume_breach_exit':
            exit_handler = str(slot.state.get('exit_handler') or '')
            phase_name = (
                exit_handler.replace('breach_', '', 1)
                if exit_handler.startswith('breach_')
                else 'phase1_initial_stop'
            )
            phase = next((p for p in self.phases if p.name == phase_name), self.phases[0])
            log.info(
                'Recover route %s → %s path=%s',
                route,
                phase.name,
                slot.path,
            )
            self._enqueue_confirmed_exit(slot, phase)
            return True
        if route == 'resume_phase3_exit':
            phase = next((p for p in self.phases if p.name == 'phase3_spx_proximity'), None)
            if phase is None:
                log.error('Phase3 not configured for recovery on %s', slot.path)
                return True
            log.info('Recover route resume_phase3_exit path=%s', slot.path)
            self._enqueue_confirmed_exit(slot, phase)
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
        mqtt_cache_stale = mqtt_cache_is_stale(self.prices)
        mon._refresh_breach_watch(streamer_stale, mqtt_cache_stale=mqtt_cache_stale)

        now = time.time()
        if now - slot.last_broker_sync >= SLOW_INTERVAL:
            slot.last_broker_sync = now
            if self._slow_broker_sync(slot):
                save_slot(slot)
                return

        if mqtt_cache_stale:
            self._lifecycle_section(slot)['breach_arm_status'] = 'stale_mqtt'
            save_slot(slot)
            return

        if streamer_stale:
            self._lifecycle_section(slot)['breach_arm_status'] = 'stale'
            save_slot(slot)
            return

        if not self._stop_is_current_for_slot(slot):
            mon._ensure_stop_for_filled_qty()
            slot.state = mon.state
            self._lifecycle_section(slot)['breach_arm_status'] = 'waiting_stop'
            save_slot(slot)
            return

        mon.kill_switch = self.prices.kill_switch
        if self._maybe_enqueue_phase3_exit(slot, mon):
            save_slot(slot)
            return

        ready, arm_status = self._breach_arm_ready(slot, mon)
        self._lifecycle_section(slot)['breach_arm_status'] = arm_status
        if not ready:
            save_slot(slot)
            return

        self._mark_breach_armed(slot, mon)

        for phase in self.phases:
            if phase.name == 'phase3_spx_proximity':
                continue
            action = phase.evaluate(mon)
            if action == PhaseAction.NONE:
                continue
            if action == PhaseAction.MAINTENANCE:
                phase.execute(mon)
                slot.state = mon.state
                save_slot(slot)
                continue
            if action == PhaseAction.EXIT_REQUIRED:
                log.info(
                    'Confirmed exit required phase=%s path=%s',
                    phase.name,
                    slot.path,
                )
                self._enqueue_confirmed_exit(slot, phase)
                save_slot(slot)
                return

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

        route = resolve_exit_recovery_route(slot)
        if route != 'none':
            if self._apply_recovery_route(slot, route):
                return

        if slot.status == 'closing':
            self._poll_closing(slot)
            return

        if slot.status == 'open':
            self._scan_open_slot(slot)

    def _cycle(self) -> None:
        duplicates = scan_duplicate_stop_ownership()
        apply_ownership_conflict_flags(duplicates)
        self._ownership_conflict_paths = {
            p for dup in duplicates for p in dup.paths
        }

        slots = self._discover_slots()
        killswitch = check_killswitch_global()
        if killswitch and not self._killswitch_claimed:
            self._killswitch_claimed = claim_killswitch()
        if not killswitch:
            self._killswitch_claimed = False

        for slot in slots:
            self._scan_slot(slot, killswitch_active=killswitch)

        self._maybe_capture_mqtt_settlement()
        self._write_heartbeat(len(slots))

    def _maybe_capture_mqtt_settlement(self) -> None:
        """Snapshot MQTT SPX at/after 15:00 CT once per session for expiry settlement."""
        try:
            from meic0dte.app.utilities import central_now
            from common.expiry_settlement import capture_mqtt_settlement_close

            today = central_now().date()
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
            spx = capture_mqtt_settlement_close(today, root=root)
            if spx is not None and not getattr(self, '_mqtt_settle_logged', False):
                log.info('MQTT settlement SPX captured for %s: %.2f', today.isoformat(), spx)
                self._mqtt_settle_logged = True
        except Exception:
            log.debug('MQTT settlement capture skipped', exc_info=True)
