"""Tests for launcher service health checks."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.service_health import (
    _read_heartbeat_json,
    check_mqtt_cache_health,
    check_stop_monitor_health,
    check_streamer_health,
)


def test_stop_monitor_absent_heartbeat(tmp_path):
    ok, detail = check_stop_monitor_health(str(tmp_path))
    assert ok is False
    assert 'absent' in detail.lower()


def test_stop_monitor_fresh_heartbeat(tmp_path):
    hb = tmp_path / 'trades' / 'heartbeat.json'
    hb.parent.mkdir(parents=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    hb.write_text(json.dumps({'ts': ts, 'loop_count': 10}), encoding='utf-8')
    ok, _ = check_stop_monitor_health(str(tmp_path))
    assert ok is True


def test_stop_monitor_corrupt_then_valid_read(tmp_path):
    hb = tmp_path / 'trades' / 'heartbeat.json'
    hb.parent.mkdir(parents=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    payload = json.dumps({'ts': ts, 'loop_count': 10})

    calls = {'n': 0}

    real_open = open

    def flaky_open(path, *args, **kwargs):
        calls['n'] += 1
        if calls['n'] == 1:
            raise json.JSONDecodeError('corrupt', '', 0)
        return real_open(path, *args, **kwargs)

    hb.write_text(payload, encoding='utf-8')
    with patch('builtins.open', side_effect=flaky_open):
        ok, detail = check_stop_monitor_health(str(tmp_path))
    assert ok is True
    assert detail == 'ok'


def test_stop_monitor_corrupt_after_retry(tmp_path):
    hb = tmp_path / 'trades' / 'heartbeat.json'
    hb.parent.mkdir(parents=True)
    hb.write_text('{not json', encoding='utf-8')
    ok, detail = check_stop_monitor_health(str(tmp_path))
    assert ok is False
    assert 'unreadable' in detail.lower()


def test_atomic_heartbeat_write_survives_concurrent_reads(tmp_path):
    hb = tmp_path / 'trades' / 'heartbeat.json'
    hb.parent.mkdir(parents=True)
    ts = datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')
    failures = []
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            tmp = hb.with_suffix('.json.tmp')
            payload = {'ts': ts, 'loop_count': 1}
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(payload, f)
                f.flush()
            os.replace(tmp, hb)

    def reader():
        while not stop.is_set():
            data, err = _read_heartbeat_json(str(hb))
            if err == 'unreadable':
                failures.append(err)
            time.sleep(0.001)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    time.sleep(0.5)
    stop.set()
    t1.join()
    t2.join()
    assert failures == []


def test_mqtt_cache_health_fresh(tmp_path):
    path = tmp_path / 'trades' / 'mqtt_cache_health.json'
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({'connected': True, 'stale': False, 'age_seconds': 1.0, 'running': True}),
        encoding='utf-8',
    )
    ok, detail = check_mqtt_cache_health(str(tmp_path))
    assert ok is True
    assert detail == 'ok'


def test_mqtt_cache_health_stale(tmp_path):
    path = tmp_path / 'trades' / 'mqtt_cache_health.json'
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({'connected': True, 'stale': True, 'age_seconds': 45.0, 'running': True}),
        encoding='utf-8',
    )
    ok, detail = check_mqtt_cache_health(str(tmp_path))
    assert ok is False
    assert 'stale' in detail.lower()


def test_streamer_stale_without_health(tmp_path):
    ok, detail = check_streamer_health(str(tmp_path))
    assert ok is False
    assert 'STREAMER' in detail
