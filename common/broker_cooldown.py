"""Broker REST cooldown circuit breaker — shared across processes via JSON file."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_COOLDOWN_PATH = os.path.join(ROOT, 'runtime', 'broker_cooldown.json')

_COOLDOWN_SEC = float(os.environ.get('TT_BROKER_COOLDOWN_SEC', '300'))


def _cooldown_path() -> str:
    return os.environ.get('MEIC_BROKER_COOLDOWN_PATH', DEFAULT_COOLDOWN_PATH)


def _read() -> Dict[str, Any]:
    path = _cooldown_path()
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write(data: Dict[str, Any]) -> None:
    path = _cooldown_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)


def cooldown_active(*, now: Optional[float] = None) -> bool:
    data = _read()
    until = float(data.get('until') or 0)
    return until > (now if now is not None else time.time())


def cooldown_until() -> float:
    return float(_read().get('until') or 0)


def cooldown_snapshot() -> Dict[str, Any]:
    data = _read()
    now = time.time()
    until = float(data.get('until') or 0)
    return {
        'active': until > now,
        'until': until,
        'remaining_sec': max(0.0, until - now),
        'reason': data.get('reason'),
        'source': data.get('source'),
        'set_at': data.get('set_at'),
    }


def set_cooldown(
    reason: str,
    *,
    source: str = 'broker',
    duration_sec: Optional[float] = None,
) -> None:
    dur = _COOLDOWN_SEC if duration_sec is None else duration_sec
    until = time.time() + dur
    data = {
        'until': until,
        'reason': reason,
        'source': source,
        'set_at': time.time(),
    }
    _write(data)
    log.warning('Broker cooldown set %ss reason=%s source=%s', dur, reason, source)


def clear_cooldown() -> None:
    path = _cooldown_path()
    try:
        os.remove(path)
    except OSError:
        pass


def should_skip_priority(priority: str) -> bool:
    """HIGH may proceed during cooldown; NORMAL/LOW are skipped."""
    if not cooldown_active():
        return False
    return priority != 'HIGH'
