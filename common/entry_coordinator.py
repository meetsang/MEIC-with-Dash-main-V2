"""Launcher-owned background entry monitor coordinator.

Runs EntryMonitorRunner.tick() on a dedicated thread so the launcher main loop
never blocks on session CSV I/O or spawn bookkeeping.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from blocks.entry.runner import EntryMonitorRunner
from common.trades_layout import ops_path

log = logging.getLogger(__name__)

ENTRY_MONITOR_HEALTH_FILE = 'entry_monitor_health.json'

_COORDINATOR: Optional['EntryMonitorCoordinator'] = None
_COORD_LOCK = threading.Lock()


def coordinator_enabled() -> bool:
    return os.environ.get('ENTRY_MONITOR_COORDINATOR_ENABLED', 'true').lower() in (
        '1', 'true', 'yes',
    )


def tick_interval_sec() -> float:
    return float(os.environ.get('ENTRY_MONITOR_TICK_INTERVAL_SEC', '1.0'))


def stall_warn_sec() -> float:
    return float(os.environ.get('ENTRY_MONITOR_STALL_WARN_SEC', '30'))


def tick_slow_warn_sec() -> float:
    return float(os.environ.get('ENTRY_MONITOR_TICK_SLOW_SEC', '10'))


def heartbeat_log_sec() -> float:
    return float(os.environ.get('ENTRY_MONITOR_HEARTBEAT_LOG_SEC', '60'))


def write_entry_monitor_health(
    *,
    root: Optional[str],
    tick_count: int,
    last_tick_duration_sec: float,
    pending_meic: int = 0,
    active_workers: int = 0,
) -> None:
    """Atomically publish entry monitor liveness for launcher/dashboard."""
    path = ops_path(ENTRY_MONITOR_HEALTH_FILE, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        'last_tick_epoch': time.time(),
        'last_tick_duration_sec': round(last_tick_duration_sec, 4),
        'tick_count': tick_count,
        'pending_meic': pending_meic,
        'active_workers': active_workers,
    }
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.flush()
    os.replace(tmp, path)


class EntryMonitorCoordinator:
    """Background thread: session CSV poll + MEIC/manual entry worker spawn."""

    def __init__(
        self,
        runner: EntryMonitorRunner,
        *,
        root: Optional[str] = None,
        logger: Optional[logging.Logger] = None,
        clock: Optional[Callable[[], datetime]] = None,
        interval_sec: Optional[float] = None,
    ):
        self.runner = runner
        self.root = root
        self.log = logger or log
        self._clock = clock
        self.interval_sec = interval_sec if interval_sec is not None else tick_interval_sec()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._stats_lock = threading.Lock()
        self._last_tick_completed_mono = 0.0
        self._last_tick_duration_sec = 0.0
        self._tick_count = 0
        self._last_heartbeat_log_mono = 0.0
        self._last_stall_warn_mono = 0.0
        self._last_slow_warn_mono = 0.0
        self._stall_warn_cooldown_sec = 60.0

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        from meic0dte.app.utilities import central_now

        return central_now()

    def start(self) -> None:
        if not coordinator_enabled():
            self.log.info('Entry monitor coordinator disabled — synchronous tick only')
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name='entry-monitor-coordinator',
            daemon=True,
        )
        self._thread.start()
        self.log.info(
            'Entry monitor coordinator started interval_sec=%.1f',
            self.interval_sec,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def check_stall(self) -> None:
        """Non-blocking stall check — call from launcher main loop each iteration."""
        with self._stats_lock:
            last = self._last_tick_completed_mono
            duration = self._last_tick_duration_sec
        if last <= 0:
            return
        now_mono = time.monotonic()
        gap = now_mono - last
        stall_sec = stall_warn_sec()
        slow_sec = tick_slow_warn_sec()
        if gap > stall_sec and now_mono - self._last_stall_warn_mono >= self._stall_warn_cooldown_sec:
            self._last_stall_warn_mono = now_mono
            self.log.critical(
                'ENTRY_MONITOR_STALL gap_sec=%.1f threshold=%.1f',
                gap,
                stall_sec,
            )
        if duration > slow_sec and now_mono - self._last_slow_warn_mono >= self._stall_warn_cooldown_sec:
            self._last_slow_warn_mono = now_mono
            self.log.critical(
                'ENTRY_MONITOR_TICK_SLOW duration_sec=%.1f threshold=%.1f',
                duration,
                slow_sec,
            )

    def _pending_meic_count(self) -> int:
        try:
            from blocks.session.plan import load_meic_session_today

            plan = load_meic_session_today(self.root)
            if plan is None:
                return 0
            return sum(
                1 for row in plan.rows
                if row.state == 'pending' and not row.paused and not row.skip
            )
        except Exception:
            return 0

    def _run_loop(self) -> None:
        try:
            while not self._stop.is_set():
                tick_start = time.monotonic()
                try:
                    self.runner.tick(self._now())
                except Exception:
                    self.log.exception('entry monitor tick failed')
                else:
                    duration = time.monotonic() - tick_start
                    with self._stats_lock:
                        self._last_tick_completed_mono = time.monotonic()
                        self._last_tick_duration_sec = duration
                        self._tick_count += 1
                        tick_count = self._tick_count
                    try:
                        write_entry_monitor_health(
                            root=self.root,
                            tick_count=tick_count,
                            last_tick_duration_sec=duration,
                            pending_meic=self._pending_meic_count(),
                            active_workers=len(self.runner._handles),
                        )
                    except Exception:
                        self.log.exception('entry monitor health write failed')
                    now_mono = time.monotonic()
                    if now_mono - self._last_heartbeat_log_mono >= heartbeat_log_sec():
                        self._last_heartbeat_log_mono = now_mono
                        self.log.info(
                            'ENTRY_MONITOR heartbeat ticks=%d pending_meic=%d workers=%d last_tick_ms=%.0f',
                            tick_count,
                            self._pending_meic_count(),
                            len(self.runner._handles),
                            duration * 1000,
                        )
                self._stop.wait(self.interval_sec)
        except Exception:
            self.log.exception('entry monitor coordinator crashed')


def get_entry_coordinator() -> Optional[EntryMonitorCoordinator]:
    return _COORDINATOR


def start_entry_coordinator(
    runner: EntryMonitorRunner,
    *,
    root: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Optional[EntryMonitorCoordinator]:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is not None:
            _COORDINATOR.stop()
        coord = EntryMonitorCoordinator(runner, root=root, logger=logger)
        coord.start()
        _COORDINATOR = coord
        return coord


def stop_entry_coordinator() -> None:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is not None:
            _COORDINATOR.stop()
            _COORDINATOR = None
