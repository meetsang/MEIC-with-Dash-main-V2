"""Streamer liveness file — G6 staleness guard."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from common.trades_layout import ops_path

STREAMER_HEALTH_FILE = 'streamer_health.json'
STALE_THRESHOLD_SEC = 30


def health_path(root: Optional[str] = None) -> str:
    return ops_path(STREAMER_HEALTH_FILE, root)


def write_health(
    *,
    last_spx_price_ts: Optional[str],
    symbols_subscribed: int = 0,
    status: str = 'live',
    root: Optional[str] = None,
    ladder_enabled: Optional[bool] = None,
    ladder_symbol_count: int = 0,
    total_subscribed_symbols: int = 0,
    ladder_last_update: Optional[str] = None,
    ladder_last_error: Optional[str] = None,
) -> None:
    payload = {
        'ts': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
        'last_spx_price_ts': last_spx_price_ts,
        'symbols_subscribed': symbols_subscribed,
        'status': status,
        'ladder_enabled': ladder_enabled,
        'ladder_symbol_count': ladder_symbol_count,
        'total_subscribed_symbols': total_subscribed_symbols or symbols_subscribed,
        'ladder_last_update': ladder_last_update,
        'ladder_last_error': ladder_last_error,
    }
    path = health_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def read_health(root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    path = health_path(root)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def spx_price_age_sec(root: Optional[str] = None) -> Optional[float]:
    """Seconds since last SPX price update; None if unknown."""
    data = read_health(root)
    if not data:
        return None
    ts = data.get('last_spx_price_ts') or data.get('ts')
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def is_stale(root: Optional[str] = None, *, threshold_sec: float = STALE_THRESHOLD_SEC) -> bool:
    age = spx_price_age_sec(root)
    if age is None:
        return True
    return age > threshold_sec
