"""Launcher-owned background REST probe coordinator.

Schedules at most: 1 startup + 1 probe per MEIC tranche per session day.
Never called synchronously from the launcher main loop or EntryMonitorRunner.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

ProbeKey = Tuple[str, str, str]  # session_date, strategy, tranche_id ('' for startup)


def coordinator_enabled() -> bool:
    return os.environ.get('REST_PROBE_COORDINATOR_ENABLED', 'true').lower() in (
        '1', 'true', 'yes',
    )


def pre_tranche_lead_sec() -> float:
    return float(os.environ.get('PRE_TRANCHE_PROBE_LEAD_SEC', '30'))


def _probe_timeout_sec() -> float:
    return float(os.environ.get('REST_PROBE_TIMEOUT_SEC', '10'))


@dataclass(frozen=True)
class TrancheWindow:
    strategy: str
    tranche_id: str
    window_start: dt_time
    window_end: dt_time


class ProbeCoordinator:
    """Background thread: shared broker + budgeted automatic probes."""

    def __init__(
        self,
        *,
        session_date_ct: str,
        paper: bool = False,
        logger: Optional[logging.Logger] = None,
        get_broker_fn: Optional[Callable[[], Any]] = None,
        run_probe_fn: Optional[Callable[..., Any]] = None,
        clock: Optional[Callable[[], datetime]] = None,
        tick_interval_sec: float = 1.0,
    ):
        self.session_date_ct = session_date_ct
        self.paper = paper
        self.log = logger or log
        self._get_broker_fn = get_broker_fn
        self._run_probe_fn = run_probe_fn
        self._clock = clock
        self.tick_interval_sec = tick_interval_sec
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._broker = None
        self._tranches: List[TrancheWindow] = []
        # Keys we have started (or completed) — never auto-retry
        self._started: set[ProbeKey] = set()
        self._completed: set[ProbeKey] = set()
        self._startup_scheduled = False

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock()
        from meic0dte.app.utilities import central_now

        return central_now()

    def set_tranches(self, tranches: List[TrancheWindow]) -> None:
        with self._lock:
            self._tranches = list(tranches)

    def start(self) -> None:
        if not coordinator_enabled():
            self.log.info('REST probe coordinator disabled')
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name='rest-probe-coordinator',
            daemon=True,
        )
        self._thread.start()
        self.log.info(
            'REST probe coordinator started session=%s lead_sec=%s',
            self.session_date_ct,
            pre_tranche_lead_sec(),
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def automatic_probe_count(self) -> int:
        with self._lock:
            return len(self._completed)

    def _startup_key(self) -> ProbeKey:
        return (self.session_date_ct, '', '')

    def _tranche_key(self, strategy: str, tranche_id: str) -> ProbeKey:
        return (self.session_date_ct, strategy, tranche_id)

    def _run_loop(self) -> None:
        try:
            self._ensure_startup_probe()
            while not self._stop.is_set():
                try:
                    self._schedule_due_tranche_probes()
                except Exception:
                    self.log.exception('probe coordinator tick failed')
                self._stop.wait(self.tick_interval_sec)
        except Exception:
            self.log.exception('probe coordinator crashed')

    def _ensure_broker(self):
        if self._broker is not None:
            return self._broker
        if self._get_broker_fn is not None:
            self._broker = self._get_broker_fn()
        else:
            from common.broker_factory import get_shared_broker

            self._broker = get_shared_broker(paper=self.paper)
        return self._broker

    def _ensure_startup_probe(self) -> None:
        if os.environ.get('REST_PROBE_ON_SESSION_START', 'true').lower() not in (
            '1', 'true', 'yes',
        ):
            return
        key = self._startup_key()
        with self._lock:
            if key in self._started:
                return
            self._started.add(key)
            self._startup_scheduled = True
        from common.trading_gate import mark_probe_scheduled

        mark_probe_scheduled(
            source='startup',
            session_date_ct=self.session_date_ct,
            strategy='',
            tranche_id='',
        )
        threading.Thread(
            target=self._execute_probe,
            kwargs={'key': key, 'source': 'startup', 'strategy': '', 'tranche_id': ''},
            name='rest-probe-startup',
            daemon=True,
        ).start()

    def _schedule_due_tranche_probes(self) -> None:
        now = self._now()
        lead = pre_tranche_lead_sec()
        with self._lock:
            tranches = list(self._tranches)
        for tw in tranches:
            key = self._tranche_key(tw.strategy, tw.tranche_id)
            with self._lock:
                if key in self._started:
                    continue
            # Build datetime for window start today
            start_dt = now.replace(
                hour=tw.window_start.hour,
                minute=tw.window_start.minute,
                second=tw.window_start.second,
                microsecond=0,
            )
            due_at = start_dt - timedelta(seconds=lead)
            if now < due_at:
                continue
            # Do not schedule after window already ended
            end_dt = now.replace(
                hour=tw.window_end.hour,
                minute=tw.window_end.minute,
                second=getattr(tw.window_end, 'second', 0) or 0,
                microsecond=0,
            )
            if now > end_dt:
                continue
            with self._lock:
                if key in self._started:
                    continue
                self._started.add(key)
            from common.trading_gate import mark_probe_scheduled

            mark_probe_scheduled(
                source='pre_tranche',
                session_date_ct=self.session_date_ct,
                strategy=tw.strategy,
                tranche_id=tw.tranche_id,
            )
            threading.Thread(
                target=self._execute_probe,
                kwargs={
                    'key': key,
                    'source': 'pre_tranche',
                    'strategy': tw.strategy,
                    'tranche_id': tw.tranche_id,
                },
                name=f'rest-probe-{tw.tranche_id}',
                daemon=True,
            ).start()

    def _execute_probe(
        self,
        *,
        key: ProbeKey,
        source: str,
        strategy: str,
        tranche_id: str,
    ) -> None:
        from common.rest_probe import RestProbeResult, run_rest_probe
        from common.trading_gate import mark_probe_running, record_probe_result

        mark_probe_running(
            source=source,
            session_date_ct=self.session_date_ct,
            strategy=strategy,
            tranche_id=tranche_id,
        )
        self.log.info(
            'REST probe starting source=%s tranche=%s strategy=%s',
            source,
            tranche_id or '(startup)',
            strategy or '-',
        )
        attempted = time.time()
        try:
            broker = self._ensure_broker()
            if self._run_probe_fn is not None:
                result = self._run_probe_fn(
                    broker,
                    source=source,
                    strategy=strategy,
                    tranche_id=tranche_id,
                )
                if not getattr(result, '_already_recorded', False):
                    record_probe_result(result)
            else:
                result = run_rest_probe(
                    broker,
                    bypass_local_cooldown=False,
                    source=source,
                    strategy=strategy,
                    tranche_id=tranche_id,
                    session_date_ct=self.session_date_ct,
                )
        except Exception as exc:
            completed = time.time()
            from common.rest_probe import classify_rest_exception

            status, http_status = classify_rest_exception(exc)
            result = RestProbeResult(
                ok=False,
                status=status,
                attempted_at_epoch=attempted,
                completed_at_epoch=completed,
                latency_ms=int((completed - attempted) * 1000),
                http_status=http_status,
                detail=str(exc),
                source=source,
                strategy=strategy,
                tranche_id=tranche_id,
                session_date_ct=self.session_date_ct,
                performed=True,
            )
            record_probe_result(result)
        with self._lock:
            self._completed.add(key)
        self.log.info(
            'REST probe finished source=%s tranche=%s ok=%s status=%s latency_ms=%s',
            source,
            tranche_id or '(startup)',
            getattr(result, 'ok', None),
            getattr(result, 'status', None),
            getattr(result, 'latency_ms', None),
        )


_COORDINATOR: Optional[ProbeCoordinator] = None
_COORD_LOCK = threading.Lock()


def get_coordinator() -> Optional[ProbeCoordinator]:
    return _COORDINATOR


def start_coordinator(
    *,
    session_date_ct: str,
    tranches: List[TrancheWindow],
    paper: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Optional[ProbeCoordinator]:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is not None:
            _COORDINATOR.stop()
        coord = ProbeCoordinator(
            session_date_ct=session_date_ct,
            paper=paper,
            logger=logger,
        )
        coord.set_tranches(tranches)
        coord.start()
        _COORDINATOR = coord
        return coord


def stop_coordinator() -> None:
    global _COORDINATOR
    with _COORD_LOCK:
        if _COORDINATOR is not None:
            _COORDINATOR.stop()
            _COORDINATOR = None


def meic_tranches_from_slots(slots) -> List[TrancheWindow]:
    """Build tranche windows from MEIC TrancheSlot list (dedup by lot)."""
    from common import trades_layout

    out: List[TrancheWindow] = []
    seen: set[str] = set()
    for slot in slots:
        lot = getattr(slot, 'lot', None) or ''
        if not lot or lot in seen:
            continue
        seen.add(lot)
        out.append(TrancheWindow(
            strategy=getattr(slot, 'strategy_name', None) or trades_layout.STRATEGY_MEIC,
            tranche_id=lot,
            window_start=slot.window_start,
            window_end=slot.window_end,
        ))
    return out


def meic_tranches_from_session_plan(plan) -> List[TrancheWindow]:
    """Eligible tranches: lot with at least one pending, unpaused, unskipped side."""
    from common import trades_layout

    by_lot: Dict[str, TrancheWindow] = {}
    pending_lots: set[str] = set()
    for row in plan.rows:
        lot = row.lot
        if lot not in by_lot:
            by_lot[lot] = TrancheWindow(
                strategy=trades_layout.STRATEGY_MEIC,
                tranche_id=lot,
                window_start=row.window_start_time(),
                window_end=row.window_end_time(),
            )
        if row.state == 'pending' and not row.paused and not row.skip:
            pending_lots.add(lot)
    return [by_lot[lot] for lot in by_lot if lot in pending_lots]
