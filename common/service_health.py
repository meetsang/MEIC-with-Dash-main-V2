"""Broad service health checks for launcher terminal alerts."""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from common.streamer_health import is_stale as streamer_is_stale, spx_price_age_sec

STOP_MONITOR_STALE_SEC = 60
STREAMER_STALE_SEC = 60
MQTT_CACHE_STALE_SEC = 30


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


def _read_heartbeat_json(path: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Return (data, error_kind). error_kind: absent | unreadable."""
    for attempt in range(2):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f), None
        except FileNotFoundError:
            return None, 'absent'
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.05)
                continue
            return None, 'unreadable'
        except OSError:
            if attempt == 0:
                time.sleep(0.05)
                continue
            return None, 'unreadable'
    return None, 'unreadable'


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
    data, err = _read_heartbeat_json(path)
    if err == 'absent':
        return False, 'STOP_MONITOR — heartbeat file absent (see logs/stop_monitor_*.log)'
    if err == 'unreadable' or data is None:
        return False, 'STOP_MONITOR — heartbeat unreadable/write race'

    age = _age_from_iso(data.get('ts'))
    if age is None:
        return False, 'STOP_MONITOR — heartbeat unreadable'
    if age > threshold_sec:
        loops = data.get('loop_count', '?')
        return False, f'STOP_MONITOR — heartbeat stale ({age:.0f}s, loop #{loops})'
    return True, 'ok'


def _read_mqtt_cache_health(root: str) -> Optional[Dict[str, Any]]:
    path = os.path.join(root, 'trades', 'mqtt_cache_health.json')
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def check_mqtt_cache_health(
    root: str,
    *,
    threshold_sec: float = MQTT_CACHE_STALE_SEC,
) -> Tuple[bool, str]:
    """Stop-monitor MQTT cache freshness (separate from streamer_health.json)."""
    data = _read_mqtt_cache_health(root)
    if not data:
        return False, 'STOP_MONITOR MQTT — cache health file absent'
    if data.get('stale'):
        age = data.get('age_seconds')
        if age is None:
            return False, 'STOP_MONITOR MQTT — cache stale'
        return False, f'STOP_MONITOR MQTT — cache stale ({float(age):.0f}s)'
    age = data.get('age_seconds')
    if age is not None and float(age) > threshold_sec:
        return False, f'STOP_MONITOR MQTT — cache stale ({float(age):.0f}s)'
    if not data.get('connected') and data.get('running'):
        return False, 'STOP_MONITOR MQTT — not connected'
    return True, 'ok'
