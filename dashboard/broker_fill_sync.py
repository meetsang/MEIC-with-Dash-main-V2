"""Dashboard active-trade fill sync — broker-spam hardened."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from brokers.base import BrokerBase
from common import broker_cooldown

log = logging.getLogger(__name__)

_fill_sync_broker: Optional[BrokerBase] = None
_fill_sync_lock = threading.Lock()
_sync_in_progress = False
_last_fill_sync_attempt = 0.0
_fill_sync_cooldown_until = 0.0
_last_broker_error: Optional[str] = None

ReadJsonFn = Callable[[str], Optional[dict]]
IterPathsFn = Callable[[], object]


def reset_fill_sync_state() -> None:
    global _fill_sync_broker, _sync_in_progress, _last_fill_sync_attempt
    global _fill_sync_cooldown_until, _last_broker_error
    with _fill_sync_lock:
        _fill_sync_broker = None
        _sync_in_progress = False
        _last_fill_sync_attempt = 0.0
        _fill_sync_cooldown_until = 0.0
        _last_broker_error = None


def fill_sync_stats() -> dict:
    return {
        'has_cached_broker': _fill_sync_broker is not None,
        'sync_in_progress': _sync_in_progress,
        'last_attempt': _last_fill_sync_attempt,
        'cooldown_until': _fill_sync_cooldown_until,
        'last_broker_error': _last_broker_error,
        'broker_cooldown': broker_cooldown.cooldown_snapshot(),
    }


def maybe_sync_active_trades(
    *,
    read_json: ReadJsonFn,
    iter_paths: IterPathsFn,
    get_broker_fn: Callable[[], BrokerBase],
    sync_fn: Callable[[BrokerBase], None],
) -> None:
    """
    Sync open-order fills only when needed. Safe for 2–3s dashboard polling.
    """
    global _fill_sync_broker, _sync_in_progress, _last_fill_sync_attempt
    global _fill_sync_cooldown_until, _last_broker_error

    from blocks.stop.pending_fill_sync import needs_open_order_sync

    needs_sync = False
    for path in iter_paths():
        state = read_json(path)
        if state and isinstance(state, dict) and needs_open_order_sync(state):
            needs_sync = True
            break

    if not needs_sync:
        return

    now = time.time()
    if now < _fill_sync_cooldown_until:
        return
    if broker_cooldown.should_skip_priority('LOW'):
        return

    if not _fill_sync_lock.acquire(blocking=False):
        return

    try:
        if _sync_in_progress:
            return
        _sync_in_progress = True
        _last_fill_sync_attempt = now

        if _fill_sync_broker is None:
            try:
                _fill_sync_broker = get_broker_fn()
            except Exception as exc:
                _last_broker_error = str(exc)
                _fill_sync_cooldown_until = now + 60.0
                log.exception('Dashboard fill sync: broker unavailable')
                return

        try:
            sync_fn(_fill_sync_broker)
            _last_broker_error = None
        except Exception as exc:
            _last_broker_error = str(exc)
            err = str(exc).lower()
            if any(tok in err for tok in ('401', '429', 'timeout', 'unauthorized', 'rate', 'block')):
                broker_cooldown.set_cooldown(str(exc), source='dashboard_fill_sync', duration_sec=300)
                _fill_sync_cooldown_until = time.time() + 300.0
            else:
                _fill_sync_cooldown_until = time.time() + 60.0
            log.exception('Dashboard fill sync failed')
    finally:
        _sync_in_progress = False
        _fill_sync_lock.release()
