"""Parallel supervisor for stop_monitor threads."""
from __future__ import annotations

import glob
import json
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from brokers.base import BrokerBase
from common import tt_config
from blocks.stop.alerts import AlertListener
from blocks.stop.expiry_gate import try_settle_or_freeze_trade
from blocks.stop.monitor import StopMonitor
from blocks.stop.mqtt_prices import get_shared_cache
from blocks.stop.pending_fill_sync import sync_pending_fills
from blocks.stop.phases import PhaseBase, default_phases
from blocks.stop import state as state_mod

log = logging.getLogger(__name__)


@dataclass
class _MonitorHandle:
    path: str
    thread: threading.Thread
    monitor: StopMonitor
    restarts: int = 0


class MonitorRunner:
    """Watch trades/active/ and run one StopMonitor thread per JSON file."""

    def __init__(
        self,
        broker: BrokerBase,
        phases: Optional[List[PhaseBase]] = None,
        watch_dir: Optional[str] = None,
        poll_interval: float = 5.0,
        alert_listener: Optional[AlertListener] = None,
    ):
        self.broker = broker
        self.phases = phases or default_phases()
        self.watch_dir = watch_dir or state_mod.active_dir()
        self.poll_interval = poll_interval
        self.alert_listener = alert_listener
        self.prices = get_shared_cache()
        if not self.prices.is_running():
            self.prices.start()
        self._handles: Dict[str, _MonitorHandle] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        state_mod.ensure_dirs()

    def _write_heartbeat(self, loop_count: int, active_trades: int) -> None:
        try:
            root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
            hb_path = os.path.join(root, 'trades', 'heartbeat.json')
            os.makedirs(os.path.dirname(hb_path), exist_ok=True)
            payload = {
                'ts': state_mod.now_iso(),
                'loop_count': loop_count,
                'active_trades': active_trades,
            }
            tmp = f'{hb_path}.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
                f.flush()
            os.replace(tmp, hb_path)
        except Exception:
            pass

    def run_forever(self) -> None:
        self.prices.start()
        if self.alert_listener:
            self.alert_listener.start()

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        log.info('MonitorRunner watching %s', ', '.join(state_mod.all_active_dirs()))
        supervisor = threading.Thread(target=self._supervise, daemon=True)
        supervisor.start()

        _loop_count = 0
        while not self._stop.is_set():
            _loop_count += 1
            self._scan_for_new()
            self._prime_peaceful_snapshot()
            with self._lock:
                active_count = len(self._handles)
            self._write_heartbeat(_loop_count, active_count)
            self._stop.wait(3)

        self.shutdown()

    def _signal_handler(self, signum, frame):
        log.info('Signal %s — shutting down MonitorRunner', signum)
        self._stop.set()

    def shutdown(self) -> None:
        with self._lock:
            for handle in self._handles.values():
                handle.monitor.stop()
        self.prices.stop()
        if self.alert_listener:
            self.alert_listener.stop()
        log.info('MonitorRunner stopped')

    def _prime_peaceful_snapshot(self) -> None:
        """One shared get_live_orders snapshot per supervisor cycle for V2 monitors."""
        from common.broker_cooldown import should_skip_priority
        from common.rest_operations import PRIORITY_LOW
        from blocks.stop.batched_reconcile import fetch_live_orders_snapshot

        if should_skip_priority(PRIORITY_LOW):
            return
        try:
            snapshot = fetch_live_orders_snapshot(self.broker)
        except Exception:
            log.exception('V2 peaceful live-orders snapshot failed')
            return
        with self._lock:
            for handle in self._handles.values():
                handle.monitor._peaceful_live_orders = snapshot

    def _sync_pending_fills(self) -> None:
        """Promote brokerage fills to open before we decide whether to start a monitor."""
        try:
            sync_pending_fills(self.broker)
        except Exception:
            log.exception('Pending fill sync failed')

    def _scan_for_new(self) -> None:
        self._sync_pending_fills()
        for path in state_mod.iter_active_trade_paths():
            with self._lock:
                if path in self._handles:
                    continue
            self.add(path)

    def add(self, json_path: str) -> None:
        try:
            st = state_mod.load_state(json_path)
        except Exception as exc:
            log.warning('Skip monitor for %s — cannot load state: %s', json_path, exc)
            return

        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        outcome, st = try_settle_or_freeze_trade(st, path=json_path, root=root)
        if outcome != 'ok':
            state_mod.section(st, 'recovery')
            state_mod.save_state(json_path, st)
            log.info('Expiry gate %s for %s — skip monitor', outcome, json_path)
            return

        status = st.get('status')
        filled = int(st.get('filled_quantity') or 0)
        target = int(st.get('quantity') or 0)
        if status != 'open':
            log.debug('Skip monitor for %s — status=%s', json_path, status)
            return
        if target and filled < target:
            log.debug('Skip monitor for %s — partial fill %s/%s', json_path, filled, target)
            return

        fill_q = None
        try:
            active = st.get('active_stop') or {}
            oid = active.get('order_id')
            if oid and self.alert_listener:
                fill_q = self.alert_listener.register(oid)
        except Exception:
            pass

        monitor = StopMonitor(
            json_path,
            self.broker,
            self.prices,
            phases=self.phases,
            fill_queue=fill_q,
            alert_listener=self.alert_listener,
        )

        def _target():
            try:
                monitor.run(self.poll_interval)
            except Exception as exc:
                log.error('Monitor crashed for %s: %s', json_path, exc)

        thread = threading.Thread(
            target=_target,
            name=f'stop-{os.path.basename(json_path)}',
            daemon=True,
        )
        handle = _MonitorHandle(path=json_path, thread=thread, monitor=monitor)
        with self._lock:
            self._handles[json_path] = handle
        thread.start()
        log.info('Started monitor for %s', json_path)

    def _supervise(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                for path, handle in list(self._handles.items()):
                    if handle.thread.is_alive():
                        continue
                    if handle.monitor.state.get('status') in ('closed', 'cancelled'):
                        del self._handles[path]
                        continue
                    if handle.restarts >= 10:
                        log.error('Giving up on %s after 10 restarts', path)
                        del self._handles[path]
                        continue
                    handle.restarts += 1
                    log.warning('Restarting monitor %s (attempt %d)', path, handle.restarts)
                    self._handles.pop(path)
                    time.sleep(3)
                    self.add(path)
            time.sleep(5)

    def status(self) -> Dict:
        with self._lock:
            return {
                path: {
                    'alive': h.thread.is_alive(),
                    'restarts': h.restarts,
                    'status': h.monitor.state.get('status'),
                }
                for path, h in self._handles.items()
            }

    def trade_state(self, json_path: str) -> Optional[Dict]:
        """In-memory state for a monitored file (avoids disk read races)."""
        with self._lock:
            handle = self._handles.get(json_path)
            if handle:
                return handle.monitor.state
        return None
