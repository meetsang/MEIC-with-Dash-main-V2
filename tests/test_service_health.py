"""Tests for launcher service health checks."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.service_health import check_stop_monitor_health, check_streamer_health


def test_stop_monitor_missing_heartbeat(tmp_path):
    ok, detail = check_stop_monitor_health(str(tmp_path))
    assert ok is False
    assert 'missing' in detail.lower()


def test_stop_monitor_fresh_heartbeat(tmp_path):
    hb = tmp_path / 'trades' / 'heartbeat.json'
    hb.parent.mkdir(parents=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    hb.write_text(json.dumps({'ts': ts, 'loop_count': 10}), encoding='utf-8')
    ok, _ = check_stop_monitor_health(str(tmp_path))
    assert ok is True


def test_streamer_stale_without_health(tmp_path):
    ok, detail = check_streamer_health(str(tmp_path))
    assert ok is False
    assert 'STREAMER' in detail
