"""Simple moving averages and exponential moving averages (stdlib only)."""
from __future__ import annotations

from typing import Iterable, List, Optional, Sequence


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema_last(values: Sequence[float], period: int) -> Optional[float]:
    """Return the latest EMA value for a close series."""
    if period <= 0 or not values:
        return None
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(values[:period]) / period
    for price in values[period:]:
        ema = price * k + ema * (1.0 - k)
    return ema


def indicator_row(closes: Sequence[float]) -> dict:
    """Compute configured SMA/EMA columns from close history."""
    from market_data import config

    row = {}
    for p in config.SMA_PERIODS:
        val = sma(closes, p)
        row[f'sma_{p}'] = '' if val is None else round(val, 4)
    for p in config.EMA_PERIODS:
        val = ema_last(closes, p)
        row[f'ema_{p}'] = '' if val is None else round(val, 4)
    return row


def ohlc_header(symbol: str = 'SPX') -> List[str]:
    from common.market_watch import symbol_has_volume_column
    from market_data import config

    cols = ['datetime', 'open', 'high', 'low', 'close']
    if symbol_has_volume_column(symbol):
        cols.append('volume')
    cols.append('samples')
    cols.extend(f'sma_{p}' for p in config.SMA_PERIODS)
    cols.extend(f'ema_{p}' for p in config.EMA_PERIODS)
    return cols
