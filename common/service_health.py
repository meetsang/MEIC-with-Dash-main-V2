"""Broad service health checks for launcher terminal alerts."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional, Tuple

from common.streamer_health import is_stale as streamer_is_stale, spx_price_age_sec

STOP_MONITOR_STALE_SEC = 60
STREAMER_STALE_SEC = 60


def _age_from_iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(str(ts))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)).total_seconds(),
        )
    except Exception:
        return None


def check_streamer_health(root: str, *, threshold_sec: float = STREAMER_STALE_SEC) -> Tuple[bool, str]:
    """Return (ok, detail). ok=False means operator should see a terminal alert."""
    if streamer_is_stale(root, threshold_sec=threshold_sec):
        age = spx_price_age_sec(root)
        if age is None:
            return False, 'STREAMER — no SPX price (see logs/stream_pub_tt_*.log)'
        return False, f'STREAMER — SPX price stale ({age:.0f}s)'
    return True, 'ok'


def check_stop_monitor_health(root: str, *, threshold_sec: float = STOP_MONITOR_STALE_SEC) -> Tuple[bool, str]:
    path = os.path.join(root, 'trades', 'heartbeat.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False, 'STOP_MONITOR — heartbeat missing (see logs/stop_monitor_*.log)'

    age = _age_from_iso(data.get('ts'))
    if age is None:
        return False, 'STOP_MONITOR — heartbeat unreadable'
    if age > threshold_sec:
        loops = data.get('loop_count', '?')
        return False, f'STOP_MONITOR — heartbeat stale ({age:.0f}s, loop #{loops})'
    return True, 'ok'
