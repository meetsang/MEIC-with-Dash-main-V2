"""Market data recorder configuration."""
from __future__ import annotations

import os

from common.market_watch import (
    SPX_LADDER_REFRESH_SEC,
    WATCH_SYMBOLS as _WATCH_SYMBOLS,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

WATCH_SYMBOLS = _WATCH_SYMBOLS

# Legacy — index OHLC is driven by per-tick MQTT listeners, not interval polling.
POLL_INTERVAL_SEC = 30

# Snapshot all registered spread option mids to one CSV (no OHLC) — trade legs only.
OPTION_SNAPSHOT_INTERVAL_SEC = 180

# Sidecar ladder snapshot cadence (aligned with ladder JSON refresh).
SPX_LADDER_SNAPSHOT_SEC = SPX_LADDER_REFRESH_SEC

# Completed OHLC bar intervals (minutes).
BAR_INTERVALS_MIN = (1, 3, 5, 10, 30, 60)

SMA_PERIODS = (9, 20, 50, 200)
EMA_PERIODS = (9, 12, 21, 26, 50)

DATA_ROOT = os.path.join(ROOT, 'data')


def day_dir(for_date) -> str:
    """data/YYYY-MM-DD/"""
    return os.path.join(DATA_ROOT, for_date.isoformat())


def polls_path(day_path: str, symbol: str) -> str:
    """Per-tick MQTT mid log (one row per streamer publish)."""
    return os.path.join(day_path, f'{symbol}_polls.csv')


def ohlc_path(day_path: str, symbol: str, interval_min: int) -> str:
    return os.path.join(day_path, f'{symbol}_{interval_min}m.csv')


def options_quotes_path(day_path: str) -> str:
    """MEIC/Manual trade legs only — snapshot_ts,symbol,mid."""
    return os.path.join(day_path, 'options_quotes.csv')


def spx_ladder_quotes_path(day_path: str) -> str:
    """Sidecar SPX 0DTE ladder mids (and optional volume)."""
    return os.path.join(day_path, 'spx_ladder_quotes.csv')
