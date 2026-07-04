"""Market data recorder configuration."""
from __future__ import annotations

import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

# Symbols published to MQTT by streamer and consumed here.
WATCH_SYMBOLS = ('SPX', 'VIX', 'QQQ', 'VXN', 'IWM')

# Poll MQTT every N seconds for new mids.
POLL_INTERVAL_SEC = 30

# Completed OHLC bar intervals (minutes).
BAR_INTERVALS_MIN = (1, 3, 5, 10, 30, 60)

SMA_PERIODS = (9, 20, 50, 200)
EMA_PERIODS = (9, 12, 21, 26, 50)

DATA_ROOT = os.path.join(ROOT, 'data')


def day_dir(for_date) -> str:
    """data/YYYY-MM-DD/"""
    return os.path.join(DATA_ROOT, for_date.isoformat())


def polls_path(day_path: str, symbol: str) -> str:
    return os.path.join(day_path, f'{symbol}_polls.csv')


def ohlc_path(day_path: str, symbol: str, interval_min: int) -> str:
    return os.path.join(day_path, f'{symbol}_{interval_min}m.csv')
