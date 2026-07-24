"""Account-wide cross-process REST rate governor (Windows + POSIX).

Keyed by broker environment (paper/test/live) and account number.
All TastyTrade REST calls must acquire a token through this governor.
HIGH priority is scheduled ahead of NORMAL/LOW but never bypasses the
global rate ceiling.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

REST_MAX_PER_SEC = float(os.environ.get('TT_REST_MAX_PER_SEC', '1'))
REST_BURST = int(os.environ.get('TT_REST_BURST', '3'))

_PRIORITY_RANK = {'HIGH': 0, 'NORMAL': 1, 'LOW': 2}


def governor_account_key(
    *,
    paper: Optional[bool] = None,
    account: Optional[str] = None,
) -> str:
    """Stable cross-process key: ``{env}_{account}``."""
    from common import tt_config

    use_paper = tt_config.PAPER_MODE if paper is None else bool(paper)
    if use_paper:
        env = 'paper'
    elif tt_config.TT_IS_TEST:
        env = 'test'
    else:
        env = 'live'
    acct = (account if account is not None else tt_config.TT_ACCOUNT_NUMBER) or 'unknown'
    safe_acct = ''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in str(acct))
    return f'{env}_{safe_acct}'


def _governor_dir(root: Optional[str] = None) -> str:
    base = root or os.environ.get('MEIC_REST_GOVERNOR_ROOT') or ROOT
    return os.path.join(base, 'runtime', 'rest_governor')


def governor_state_path(account_key: Optional[str] = None, *, root: Optional[str] = None) -> str:
    key = account_key or governor_account_key()
    return os.path.join(_governor_dir(root), f'{key}.json')


def governor_lock_path(account_key: Optional[str] = None, *, root: Optional[str] = None) -> str:
    key = account_key or governor_account_key()
    return os.path.join(_governor_dir(root), f'{key}.lock')


def aggregate_metrics_path(account_key: Optional[str] = None, *, root: Optional[str] = None) -> str:
    key = account_key or governor_account_key()
    base = root or os.environ.get('MEIC_REST_GOVERNOR_ROOT') or ROOT
    return os.path.join(base, 'runtime', f'rest_metrics_account_{key}.json')


@contextmanager
def _file_lock(path: str, *, timeout_sec: float = 30.0) -> Iterator[None]:
    """Exclusive cross-process lock (msvcrt on Windows, fcntl elsewhere)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, 'a+b')
    deadline = time.time() + max(0.5, timeout_sec)
    locked = False
    try:
        while True:
            try:
                if sys.platform == 'win32':
                    import msvcrt

                    fh.seek(0)
                    if fh.read(1) == b'':
                        fh.write(b'\0')
                        fh.flush()
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise TimeoutError(f'rest governor lock timeout: {path}')
                time.sleep(0.01)
        yield
    finally:
        if locked:
            try:
                if sys.platform == 'win32':
                    import msvcrt

                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _empty_state() -> Dict[str, Any]:
    return {
        'timestamps': [],
        'calls_1m': [],
        'calls_5m': [],
        'total': 0,
        'waiters': [],
        'by_priority': {'HIGH': 0, 'NORMAL': 0, 'LOW': 0},
        'by_operation': {},
        'last_updated_epoch': time.time(),
    }


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            for key, default in _empty_state().items():
                data.setdefault(key, default if not isinstance(default, dict) else dict(default))
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return _empty_state()


def _save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    state['last_updated_epoch'] = time.time()
    tmp = f'{path}.{os.getpid()}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, path)


def _evict(state: Dict[str, Any], now: float) -> None:
    state['timestamps'] = [t for t in state.get('timestamps') or [] if now - float(t) <= 1.0]
    state['calls_1m'] = [t for t in state.get('calls_1m') or [] if now - float(t) <= 60.0]
    state['calls_5m'] = [t for t in state.get('calls_5m') or [] if now - float(t) <= 300.0]
    # Drop stale waiters (>60s) so crashed processes cannot block forever
    waiters = []
    for w in state.get('waiters') or []:
        if not isinstance(w, dict):
            continue
        if now - float(w.get('enqueued_at') or 0) > 60.0:
            continue
        waiters.append(w)
    state['waiters'] = waiters


def _rank(priority: str) -> int:
    return _PRIORITY_RANK.get(priority, 1)


def _best_waiter(waiters: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not waiters:
        return None
    return min(
        waiters,
        key=lambda w: (_rank(str(w.get('priority') or 'NORMAL')), float(w.get('enqueued_at') or 0)),
    )


def _token_wait_sec(state: Dict[str, Any], *, max_per_sec: float, burst: int, now: float) -> float:
    stamps = [float(t) for t in state.get('timestamps') or []]
    if len(stamps) < burst:
        return 0.0
    oldest = min(stamps)
    wait = (1.0 / max_per_sec) - (now - oldest)
    return max(0.0, wait)


def _publish_aggregate(account_key: str, state: Dict[str, Any], *, root: Optional[str] = None) -> None:
    try:
        path = aggregate_metrics_path(account_key, root=root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        payload = {
            'scope': 'account',
            'account_key': account_key,
            'total_calls': int(state.get('total') or 0),
            'calls_last_1m': len(state.get('calls_1m') or []),
            'calls_last_5m': len(state.get('calls_5m') or []),
            'by_priority': dict(state.get('by_priority') or {}),
            'by_operation': dict(state.get('by_operation') or {}),
            'waiters': len(state.get('waiters') or []),
            'last_updated_epoch': state.get('last_updated_epoch'),
        }
        tmp = f'{path}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        log.debug('aggregate rest metrics publish failed', exc_info=True)


class RestLimiter:
    """Account-wide governor with process-local metrics mirror."""

    def __init__(
        self,
        max_per_sec: float = REST_MAX_PER_SEC,
        burst: int = REST_BURST,
        *,
        account_key: Optional[str] = None,
        root: Optional[str] = None,
        cross_process: Optional[bool] = None,
    ):
        self._max_per_sec = max(0.1, max_per_sec)
        self._burst = max(1, burst)
        self._account_key = account_key or governor_account_key()
        self._root = root
        if cross_process is None:
            cross_process = os.environ.get('TT_REST_CROSS_PROCESS', 'true').lower() in (
                '1', 'true', 'yes',
            )
        self._cross_process = cross_process
        self._lock = threading.Lock()
        # Local mirror (per-process observability)
        self._timestamps: Deque[float] = deque()
        self._calls_1m: Deque[float] = deque()
        self._calls_5m: Deque[float] = deque()
        self._total = 0
        self._state_path = governor_state_path(self._account_key, root=root)
        self._lock_path = governor_lock_path(self._account_key, root=root)

    @property
    def account_key(self) -> str:
        return self._account_key

    def acquire(self, *, priority: str = 'NORMAL', name: str = '') -> bool:
        """Block until a token is granted under the account-wide ceiling."""
        from common.rest_metrics import record_call

        pr = priority if priority in _PRIORITY_RANK else 'NORMAL'
        op = name or 'broker'
        if self._cross_process:
            self._acquire_cross_process(priority=pr, name=op)
        else:
            self._acquire_local(priority=pr, name=op)
        try:
            record_call(op, pr)
        except Exception:
            pass
        return True

    def _acquire_local(self, *, priority: str, name: str) -> None:
        """In-process fallback with priority queue (tests / single process)."""
        waiter_id = str(uuid.uuid4())
        enqueued = time.time()
        with self._lock:
            if not hasattr(self, '_local_waiters'):
                self._local_waiters: List[Dict[str, Any]] = []
            self._local_waiters.append({
                'id': waiter_id,
                'priority': priority,
                'enqueued_at': enqueued,
                'name': name,
            })
        while True:
            sleep_for = 0.02
            with self._lock:
                now = time.time()
                self._evict_local(now)
                best = _best_waiter(self._local_waiters)
                if best and best.get('id') == waiter_id:
                    wait = 0.0
                    if len(self._timestamps) >= self._burst:
                        oldest = self._timestamps[0]
                        wait = (1.0 / self._max_per_sec) - (now - oldest)
                    if wait <= 0:
                        while len(self._timestamps) >= self._burst:
                            self._timestamps.popleft()
                        self._timestamps.append(now)
                        self._calls_1m.append(now)
                        self._calls_5m.append(now)
                        self._total += 1
                        self._local_waiters = [
                            w for w in self._local_waiters if w.get('id') != waiter_id
                        ]
                        return
                    sleep_for = wait
            time.sleep(min(0.25, max(0.01, sleep_for)))

    def _evict_local(self, now: float) -> None:
        while self._timestamps and now - self._timestamps[0] > 1.0:
            self._timestamps.popleft()
        while self._calls_1m and now - self._calls_1m[0] > 60.0:
            self._calls_1m.popleft()
        while self._calls_5m and now - self._calls_5m[0] > 300.0:
            self._calls_5m.popleft()

    def _acquire_cross_process(self, *, priority: str, name: str) -> None:
        waiter_id = f'{os.getpid()}-{uuid.uuid4().hex[:12]}'
        enqueued = time.time()
        while True:
            sleep_for = 0.02
            with _file_lock(self._lock_path):
                state = _load_state(self._state_path)
                now = time.time()
                _evict(state, now)
                waiters = [
                    w for w in state.get('waiters') or []
                    if isinstance(w, dict) and w.get('id') != waiter_id
                ]
                waiters.append({
                    'id': waiter_id,
                    'priority': priority,
                    'enqueued_at': enqueued,
                    'pid': os.getpid(),
                    'name': name,
                })
                state['waiters'] = waiters
                best = _best_waiter(waiters)
                token_wait = _token_wait_sec(
                    state,
                    max_per_sec=self._max_per_sec,
                    burst=self._burst,
                    now=now,
                )
                if best and best.get('id') == waiter_id and token_wait <= 0:
                    stamps = [float(t) for t in state.get('timestamps') or []]
                    if len(stamps) >= self._burst:
                        stamps = stamps[1:]
                    stamps.append(now)
                    state['timestamps'] = stamps
                    calls_1m = [float(t) for t in state.get('calls_1m') or []]
                    calls_5m = [float(t) for t in state.get('calls_5m') or []]
                    calls_1m.append(now)
                    calls_5m.append(now)
                    state['calls_1m'] = calls_1m
                    state['calls_5m'] = calls_5m
                    state['total'] = int(state.get('total') or 0) + 1
                    by_pr = dict(state.get('by_priority') or {'HIGH': 0, 'NORMAL': 0, 'LOW': 0})
                    by_pr[priority] = int(by_pr.get(priority) or 0) + 1
                    state['by_priority'] = by_pr
                    by_op = dict(state.get('by_operation') or {})
                    by_op[name] = int(by_op.get(name) or 0) + 1
                    state['by_operation'] = by_op
                    state['waiters'] = [w for w in waiters if w.get('id') != waiter_id]
                    _save_state(self._state_path, state)
                    _publish_aggregate(self._account_key, state, root=self._root)
                    with self._lock:
                        self._timestamps.append(now)
                        self._calls_1m.append(now)
                        self._calls_5m.append(now)
                        self._total += 1
                        self._evict_local(now)
                    return
                _save_state(self._state_path, state)
                sleep_for = max(0.01, min(0.25, token_wait if best and best.get('id') == waiter_id else 0.02))
            time.sleep(sleep_for)

    def stats(self) -> dict:
        from common.rest_metrics import metrics_snapshot

        with self._lock:
            now = time.time()
            self._evict_local(now)
            base = {
                'scope': 'account' if self._cross_process else 'per_process',
                'account_key': self._account_key,
                'total_calls': self._total,
                'calls_last_1m': len(self._calls_1m),
                'calls_last_5m': len(self._calls_5m),
                'max_per_sec': self._max_per_sec,
                'burst': self._burst,
                'cross_process': self._cross_process,
            }
        if self._cross_process:
            try:
                with _file_lock(self._lock_path, timeout_sec=2.0):
                    state = _load_state(self._state_path)
                    _evict(state, time.time())
                    base['account_total_calls'] = int(state.get('total') or 0)
                    base['account_calls_last_1m'] = len(state.get('calls_1m') or [])
                    base['account_calls_last_5m'] = len(state.get('calls_5m') or [])
                    base['account_by_priority'] = dict(state.get('by_priority') or {})
            except Exception:
                pass
        try:
            base['metrics'] = metrics_snapshot()
        except Exception:
            pass
        return base


_global_limiter: Optional[RestLimiter] = None
_global_lock = threading.Lock()


def get_rest_limiter() -> RestLimiter:
    global _global_limiter
    with _global_lock:
        if _global_limiter is None:
            _global_limiter = RestLimiter()
        return _global_limiter


def reset_rest_limiter() -> None:
    global _global_limiter
    with _global_lock:
        _global_limiter = None
