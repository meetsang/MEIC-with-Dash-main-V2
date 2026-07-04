"""Tests for market_data indicators and OHLC aggregation."""
from __future__ import annotations

import csv
import os
import sys
import tempfile
from datetime import datetime

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from market_data import config
from market_data.aggregator import SymbolState, _bucket_start, _is_bucket_complete
from market_data.indicators import ema_last, indicator_row, sma


def test_sma_and_ema():
    closes = [float(i) for i in range(1, 21)]
    assert sma(closes, 9) == pytest.approx(sum(closes[-9:]) / 9)
    assert sma(closes, 50) is None
    ema = ema_last(closes, 9)
    assert ema is not None
    assert 10 < ema < 20


def test_indicator_row_keys():
    closes = [100.0 + i for i in range(250)]
    row = indicator_row(closes)
    for p in config.SMA_PERIODS:
        assert f'sma_{p}' in row
    for p in config.EMA_PERIODS:
        assert f'ema_{p}' in row


def test_bucket_helpers():
    dt = datetime(2026, 7, 1, 10, 7)
    assert _bucket_start(dt, 3) == datetime(2026, 7, 1, 10, 6)
    assert _is_bucket_complete(datetime(2026, 7, 1, 10, 8), 3) is True
    assert _is_bucket_complete(datetime(2026, 7, 1, 10, 7), 3) is False


def test_symbol_state_minute_ohlc_and_rollup():
    with tempfile.TemporaryDirectory() as tmp:
        day_path = os.path.join(tmp, '2026-07-01')
        os.makedirs(day_path)
        st = SymbolState(symbol='SPX', day_path=day_path)

        base = datetime(2026, 7, 1, 10, 0, 15)
        st.record_poll(base, 6000.0)
        st.record_poll(base.replace(second=45), 6005.0)

        # New minute triggers finalize of 10:00 bar
        st.record_poll(datetime(2026, 7, 1, 10, 1, 10), 6010.0)

        path_1m = config.ohlc_path(day_path, 'SPX', 1)
        assert os.path.isfile(path_1m)
        with open(path_1m, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 1
        assert rows[0]['open'] == '6000.0'
        assert rows[0]['high'] == '6005.0'
        assert rows[0]['low'] == '6000.0'
        assert rows[0]['close'] == '6005.0'

        polls = config.polls_path(day_path, 'SPX')
        with open(polls, encoding='utf-8') as f:
            poll_rows = list(csv.DictReader(f))
        assert len(poll_rows) == 3

        # Three consecutive minutes to complete one 3m bar
        st.record_poll(datetime(2026, 7, 1, 10, 2, 5), 6012.0)
        st.record_poll(datetime(2026, 7, 1, 10, 3, 5), 6015.0)
        path_3m = config.ohlc_path(day_path, 'SPX', 3)
        assert os.path.isfile(path_3m)
        with open(path_3m, encoding='utf-8') as f:
            bars_3 = list(csv.DictReader(f))
        assert len(bars_3) == 1
        assert bars_3[0]['open'] == '6000.0'
        assert bars_3[0]['close'] == '6012.0'
