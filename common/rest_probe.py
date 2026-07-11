"""REST readiness probe — one direct uncached live-orders call."""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from common.broker_cooldown import clear_cooldown, cooldown_active

log = logging.getLogger(__name__)

_PROBE_LOCK = threading.Lock()
_LAST_PROBE_AT = 0.0


@dataclass(frozen=True)
class RestProbeResult:
    ok: bool
    status: str
    attempted_at_epoch: float
    completed_at_epoch: float
    latency_ms: int
    http_status: Optional[int]
    detail: str
    source: str = 'startup'
    operation: str = 'rest_health_probe_orders'


def _probe_min_interval_sec() -> float:
    return float(os.environ.get('REST_PROBE_MIN_INTERVAL_SEC', '10'))


def _probe_timeout_sec() -> float:
    return float(os.environ.get('REST_PROBE_TIMEOUT_SEC', '10'))


def classify_rest_exception(exc: Exception) -> tuple[str, Optional[int]]:
    msg = str(exc).lower()
    if '429' in msg or 'rate limit' in msg:
        return 'rate_limited', 429
    if any(tok in msg for tok in ('401', '403', 'unauthorized', 'forbidden')):
        code = 401 if '401' in msg else 403 if '403' in msg else None
        return 'auth_failed', code
    if 'timeout' in msg or 'timed out' in msg:
        return 'unavailable', None
    if any(tok in msg for tok in ('500', '502', '503', '504', 'network', 'connection')):
        return 'unavailable', None
    return 'unknown', None


def run_rest_probe(
    broker,
    *,
    bypass_local_cooldown: bool = False,
    source: str = 'startup',
) -> RestProbeResult:
    """Run one direct orders REST probe; updates trading_gate."""
    global _LAST_PROBE_AT

    from brokers.tastytrade_broker import BrokerCooldownActive

    now = time.time()
    with _PROBE_LOCK:
        if now - _LAST_PROBE_AT < _probe_min_interval_sec():
            from common.trading_gate import read_state

            state = read_state()
            lp = state.get('last_probe') or {}
            return RestProbeResult(
                ok=bool(lp.get('ok')),
                status=str(state.get('rest_status') or 'unknown'),
                attempted_at_epoch=float(lp.get('attempted_at_epoch') or now),
                completed_at_epoch=float(lp.get('completed_at_epoch') or now),
                latency_ms=int(lp.get('latency_ms') or 0),
                http_status=lp.get('http_status'),
                detail='probe rate-limited (min interval)',
                source=source,
            )
        _LAST_PROBE_AT = now

    attempted = time.time()

    if not bypass_local_cooldown and cooldown_active():
        completed = time.time()
        result = RestProbeResult(
            ok=False,
            status='rate_limited',
            attempted_at_epoch=attempted,
            completed_at_epoch=completed,
            latency_ms=int((completed - attempted) * 1000),
            http_status=429,
            detail='local broker cooldown active — automatic probe skipped',
            source=source,
        )
        from common.trading_gate import record_probe_result

        record_probe_result(result)
        return result

    try:
        raw = broker.probe_orders_rest(
            priority='HIGH',
            op='rest_health_probe_orders',
            bypass_local_cooldown=bypass_local_cooldown,
            timeout=_probe_timeout_sec(),
        )
        if isinstance(raw, RestProbeResult):
            result = RestProbeResult(
                ok=raw.ok,
                status=raw.status,
                attempted_at_epoch=raw.attempted_at_epoch,
                completed_at_epoch=raw.completed_at_epoch,
                latency_ms=raw.latency_ms,
                http_status=raw.http_status,
                detail=raw.detail,
                source=source,
                operation=raw.operation,
            )
        else:
            completed = time.time()
            result = RestProbeResult(
                ok=True,
                status='healthy',
                attempted_at_epoch=attempted,
                completed_at_epoch=completed,
                latency_ms=int((completed - attempted) * 1000),
                http_status=200,
                detail='',
                source=source,
            )
    except BrokerCooldownActive as exc:
        completed = time.time()
        result = RestProbeResult(
            ok=False,
            status='rate_limited',
            attempted_at_epoch=attempted,
            completed_at_epoch=completed,
            latency_ms=int((completed - attempted) * 1000),
            http_status=429,
            detail=str(exc),
            source=source,
        )
    except Exception as exc:
        status, http_status = classify_rest_exception(exc)
        completed = time.time()
        result = RestProbeResult(
            ok=False,
            status=status,
            attempted_at_epoch=attempted,
            completed_at_epoch=completed,
            latency_ms=int((completed - attempted) * 1000),
            http_status=http_status,
            detail=str(exc),
            source=source,
        )

    from common.trading_gate import record_probe_result

    record_probe_result(result)

    if result.ok and bypass_local_cooldown:
        clear_cooldown()
        log.info('REST probe succeeded — local cooldown cleared (latch unchanged)')

    return result
