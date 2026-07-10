"""Adaptive working-stop REST reconcile interval policy (V2 + V3)."""
from __future__ import annotations

import hashlib
import os
from typing import Any, Dict, Optional

STOP_RECONCILE_OPEN_SEC = float(os.environ.get('STOP_RECONCILE_OPEN_SEC', '15'))
STOP_RECONCILE_OPEN_JITTER_SEC = float(os.environ.get('STOP_RECONCILE_OPEN_JITTER_SEC', '5'))
STOP_RECONCILE_STALE_SEC = float(os.environ.get('STOP_RECONCILE_STALE_SEC', '10'))
STOP_RECONCILE_CLOSING_SEC = float(os.environ.get('STOP_RECONCILE_CLOSING_SEC', '5'))

LEGACY_SLOW_INTERVAL_SEC = float(os.environ.get('STOP_RECONCILE_LEGACY_SEC', '10'))


def _lifecycle(state: Dict[str, Any]) -> Dict[str, Any]:
    lc = state.setdefault('lifecycle', {})
    if not isinstance(lc, dict):
        lc = {}
        state['lifecycle'] = lc
    return lc


def reconcile_identity_key(state: Dict[str, Any]) -> str:
    entry = state.get('entry') or {}
    strategy = str(state.get('instrument') or entry.get('strategy') or '')
    lot = str(state.get('lot') or entry.get('lot') or '')
    side = str(entry.get('side') or '')
    oid = str((state.get('active_stop') or {}).get('order_id') or '')
    return f'{strategy}|{lot}|{side}|{oid}'


_MAX_DIGEST_VALUE = (1 << 64) - 1


def stable_reconcile_jitter_sec(state: Dict[str, Any]) -> float:
    """Deterministic sub-second jitter in [0, STOP_RECONCILE_OPEN_JITTER_SEC] — stable across restarts."""
    if STOP_RECONCILE_OPEN_JITTER_SEC <= 0:
        return 0.0
    digest = hashlib.sha256(reconcile_identity_key(state).encode('utf-8')).digest()
    digest_value = int.from_bytes(digest[:8], 'big')
    fraction = digest_value / _MAX_DIGEST_VALUE
    return fraction * STOP_RECONCILE_OPEN_JITTER_SEC


def reconcile_interval_sec(
    trade_state: Dict[str, Any],
    *,
    mqtt_healthy: bool,
    exit_job_active: bool = False,
    long_chase_active: bool = False,
    recovery_active: bool = False,
    close_only_mode: bool = False,
    status: Optional[str] = None,
) -> float:
    """Return seconds until the next peaceful working-stop reconcile is due."""
    st = str(status or trade_state.get('status') or 'open')
    if recovery_active:
        return 0.0
    if exit_job_active or long_chase_active:
        return LEGACY_SLOW_INTERVAL_SEC
    if st == 'closing' or close_only_mode:
        return STOP_RECONCILE_CLOSING_SEC
    if st != 'open':
        return STOP_RECONCILE_STALE_SEC
    if mqtt_healthy:
        return STOP_RECONCILE_OPEN_SEC + stable_reconcile_jitter_sec(trade_state)
    return STOP_RECONCILE_STALE_SEC


def next_working_stop_reconcile_epoch(state: Dict[str, Any]) -> float:
    try:
        return float(_lifecycle(state).get('next_working_stop_reconcile_epoch') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def schedule_next_working_stop_reconcile(
    state: Dict[str, Any],
    now: float,
    *,
    mqtt_healthy: bool,
    exit_job_active: bool = False,
    long_chase_active: bool = False,
    recovery_active: bool = False,
    close_only_mode: bool = False,
    status: Optional[str] = None,
) -> float:
    interval = reconcile_interval_sec(
        state,
        mqtt_healthy=mqtt_healthy,
        exit_job_active=exit_job_active,
        long_chase_active=long_chase_active,
        recovery_active=recovery_active,
        close_only_mode=close_only_mode,
        status=status,
    )
    due = now + max(0.0, interval)
    lc = _lifecycle(state)
    lc['next_working_stop_reconcile_epoch'] = due
    lc['working_stop_reconcile_interval_sec'] = interval
    lc['working_stop_reconcile_jitter_sec'] = stable_reconcile_jitter_sec(state)
    return due


def is_working_stop_reconcile_due(
    state: Dict[str, Any],
    now: float,
    *,
    mqtt_healthy: bool,
    exit_job_active: bool = False,
    long_chase_active: bool = False,
    recovery_active: bool = False,
    close_only_mode: bool = False,
    status: Optional[str] = None,
) -> bool:
    if exit_job_active or long_chase_active or recovery_active:
        return False
    due = next_working_stop_reconcile_epoch(state)
    if due <= 0:
        schedule_next_working_stop_reconcile(
            state,
            now,
            mqtt_healthy=mqtt_healthy,
            close_only_mode=close_only_mode,
            status=status,
        )
        due = next_working_stop_reconcile_epoch(state)
    return now >= due


def simulate_reconcile_events(
    trade_states: list[Dict[str, Any]],
    *,
    duration_sec: float = 600.0,
    mqtt_healthy: bool = True,
    fixed_interval_sec: Optional[float] = None,
) -> Dict[str, Any]:
    """Deterministic reconcile schedule simulation for before/after comparison."""
    import heapq

    heap: list[tuple[float, int]] = []
    for idx, st in enumerate(trade_states):
        if fixed_interval_sec is not None:
            due = 0.0
        else:
            schedule_next_working_stop_reconcile(
                st, 0.0, mqtt_healthy=mqtt_healthy, status='open',
            )
            due = next_working_stop_reconcile_epoch(st)
        heapq.heappush(heap, (due, idx))

    events: list[float] = []
    while heap and heap[0][0] <= duration_sec + 1e-9:
        due, idx = heapq.heappop(heap)
        st = trade_states[idx]
        events.append(due)
        if fixed_interval_sec is not None:
            nxt = due + fixed_interval_sec
        else:
            schedule_next_working_stop_reconcile(
                st, due, mqtt_healthy=mqtt_healthy, status='open',
            )
            nxt = next_working_stop_reconcile_epoch(st)
        if nxt <= duration_sec + 1e-9:
            heapq.heappush(heap, (nxt, idx))

    bucket: Dict[int, int] = {}
    peak = 0
    for ts in events:
        sec = int(ts)
        bucket[sec] = bucket.get(sec, 0) + 1
        peak = max(peak, bucket[sec])
    return {
        'total_calls': len(events),
        'peak_calls_one_second': peak,
        'per_second_buckets': dict(sorted(bucket.items())),
        'events': events,
    }
