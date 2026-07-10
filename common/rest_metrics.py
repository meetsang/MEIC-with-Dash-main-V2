"""Lightweight per-process REST call metrics (no broker I/O)."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Optional

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
MAX_DYNAMIC_KEYS = 32
KNOWN_OPERATIONS = (
    'pending_fill_status',
    'working_stop_reconcile',
    'entry_market_data',
    'spread_close_status',
    'long_close_status',
    'get_order',
    'get_live_orders',
    'cancel_order',
    'place_stop_order',
    'recovery_reconcile',
    'fill_audit',
)


class RestMetrics:
    """Thread-safe bounded REST metrics for one process."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._window_start_epoch = time.time()
        self._calls_1m: Deque[float] = deque()
        self._calls_5m: Deque[float] = deque()
        self._by_operation: Dict[str, int] = {k: 0 for k in KNOWN_OPERATIONS}
        self._by_priority: Dict[str, int] = {'HIGH': 0, 'NORMAL': 0, 'LOW': 0}
        self._skipped_cooldown: Dict[str, int] = {}
        self._failed: Dict[str, int] = {}
        self._last_429_epoch: Optional[float] = None
        self._pid = os.getpid()

    def _touch_time(self, now: float) -> None:
        self._calls_1m.append(now)
        self._calls_5m.append(now)
        while self._calls_1m and now - self._calls_1m[0] > 60.0:
            self._calls_1m.popleft()
        while self._calls_5m and now - self._calls_5m[0] > 300.0:
            self._calls_5m.popleft()

    def _inc_bucket(self, bucket: Dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1
        if len(bucket) > MAX_DYNAMIC_KEYS:
            for candidate in list(bucket):
                if candidate not in KNOWN_OPERATIONS:
                    bucket.pop(candidate, None)
                    break

    def record_call(self, operation: str, priority: str) -> None:
        try:
            with self._lock:
                now = time.time()
                self._touch_time(now)
                op = operation or 'unknown'
                if op not in self._by_operation:
                    self._by_operation[op] = 0
                self._by_operation[op] += 1
                pr = priority if priority in self._by_priority else 'NORMAL'
                self._by_priority[pr] += 1
        except Exception:
            log.debug('rest metrics record_call failed', exc_info=True)

    def record_skipped_cooldown(self, operation: str, priority: str) -> None:
        try:
            with self._lock:
                key = f'{operation}:{priority}'
                self._inc_bucket(self._skipped_cooldown, key)
        except Exception:
            log.debug('rest metrics record_skipped_cooldown failed', exc_info=True)

    def record_failure(self, operation: str, exc: Exception) -> None:
        try:
            with self._lock:
                key = f'{operation}:{type(exc).__name__}'
                self._inc_bucket(self._failed, key)
        except Exception:
            log.debug('rest metrics record_failure failed', exc_info=True)

    def record_429(self) -> None:
        try:
            with self._lock:
                self._last_429_epoch = time.time()
        except Exception:
            log.debug('rest metrics record_429 failed', exc_info=True)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            now = time.time()
            while self._calls_1m and now - self._calls_1m[0] > 60.0:
                self._calls_1m.popleft()
            while self._calls_5m and now - self._calls_5m[0] > 300.0:
                self._calls_5m.popleft()
            return {
                'scope': 'per_process',
                'pid': self._pid,
                'window_start_epoch': round(self._window_start_epoch, 3),
                'calls_last_1m': len(self._calls_1m),
                'calls_last_5m': len(self._calls_5m),
                'by_operation': dict(self._by_operation),
                'by_priority': dict(self._by_priority),
                'skipped_cooldown': dict(self._skipped_cooldown),
                'failed': dict(self._failed),
                'last_429_epoch': self._last_429_epoch,
            }


_global_metrics: Optional[RestMetrics] = None
_global_lock = threading.Lock()


def get_rest_metrics() -> RestMetrics:
    global _global_metrics
    with _global_lock:
        if _global_metrics is None:
            _global_metrics = RestMetrics()
        return _global_metrics


def reset_rest_metrics() -> None:
    global _global_metrics
    with _global_lock:
        _global_metrics = None


def metrics_snapshot() -> Dict[str, Any]:
    return get_rest_metrics().snapshot()


def record_call(operation: str, priority: str) -> None:
    get_rest_metrics().record_call(operation, priority)


def record_skipped_cooldown(operation: str, priority: str) -> None:
    get_rest_metrics().record_skipped_cooldown(operation, priority)


def record_failure(operation: str, exc: Exception) -> None:
    get_rest_metrics().record_failure(operation, exc)


def record_429() -> None:
    get_rest_metrics().record_429()


def metrics_path(root: Optional[str] = None) -> str:
    base = root or ROOT
    return os.path.join(base, 'runtime', f'rest_metrics_{os.getpid()}.json')


def write_metrics_snapshot(root: Optional[str] = None) -> None:
    """Atomically publish per-process REST metrics (no broker calls)."""
    try:
        path = metrics_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = metrics_snapshot()
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        log.debug('write_metrics_snapshot failed', exc_info=True)
