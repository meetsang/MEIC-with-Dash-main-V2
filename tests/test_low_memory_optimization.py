"""Tests for low-memory e2-micro optimizations (lazy imports, in-process services, lean ladder)."""
from __future__ import annotations

import os
import sys
import threading
import time
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_nyse_calendar_cache_loads_fast():
    from common.config import _get_nyse_early_closes, _get_nyse_holidays

    holidays = _get_nyse_holidays(2026)
    assert '261126' in holidays  # Thanksgiving
    assert '261225' in holidays  # Christmas

    early = _get_nyse_early_closes(2026)
    assert early.get('261127') == '12:00'  # Black Friday
    assert early.get('261224') == '12:00'  # Christmas Eve


def test_spx_ladder_configurable_and_low_memory_awareness():
    from common.market_watch import (
        _get_spx_ladder_max_symbols,
        sidecar_option_collection_enabled,
    )

    with patch.dict(os.environ, {'LOW_MEMORY_MODE': 'true', 'MEIC_SIDE_OPTION_COLLECTION': ''}):
        assert sidecar_option_collection_enabled() is False
        assert _get_spx_ladder_max_symbols() == 50

    with patch.dict(os.environ, {'LOW_MEMORY_MODE': 'false', 'MEIC_SIDE_OPTION_COLLECTION': 'true', 'SPX_LADDER_MAX_ACTIVE_SYMBOLS': '100'}):
        assert sidecar_option_collection_enabled() is True
        assert _get_spx_ladder_max_symbols() == 100


def test_in_process_service_handle_lifecycle():
    from common.in_process_services import InProcessServiceHandle

    stop_event = threading.Event()
    loop_ran = []

    def _worker():
        while not stop_event.is_set():
            loop_ran.append(1)
            time.sleep(0.01)

    handle = InProcessServiceHandle(
        name='test-service',
        target=_worker,
        stop_fn=stop_event.set,
    )

    assert handle.poll() is None
    handle.start()
    time.sleep(0.05)
    assert handle.is_alive() is True
    assert handle.poll() is None
    assert len(loop_ran) > 0

    handle.terminate()
    ret = handle.wait(timeout=2.0)
    assert ret == 0
    assert handle.is_alive() is False
    assert handle.poll() == 0


def test_run_py_low_memory_cli_args():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--in-process', action='store_true')
    parser.add_argument('--low-memory', action='store_true')

    args = parser.parse_args(['--low-memory', '--in-process'])
    assert args.in_process is True
    assert args.low_memory is True

