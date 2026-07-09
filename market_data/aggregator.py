"""OHLC bar aggregation and CSV persistence."""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple

from common.market_watch import symbol_has_volume_column
from market_data import config
from market_data.indicators import indicator_row, ohlc_header


def _bucket_start(dt: datetime, interval_min: int) -> datetime:
    dt = dt.replace(second=0, microsecond=0)
    offset = (dt.hour * 60 + dt.minute) % interval_min
    return dt - timedelta(minutes=offset)


def _is_bucket_complete(minute_start: datetime, interval_min: int) -> bool:
    minute_of_day = minute_start.hour * 60 + minute_start.minute
    return (minute_of_day % interval_min) == (interval_min - 1)


@dataclass
class OhlcBar:
    interval_min: int
    start: datetime
    open: float
    high: float
    low: float
    close: float
    samples: int = 0
    volume: int = 0
    track_volume: bool = False

    def absorb(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.samples += 1

    def absorb_volume(self, size: int) -> None:
        if size > 0:
            self.volume += int(size)

    def to_row(self, closes_history: Sequence[float], *, symbol: str) -> dict:
        series = list(closes_history) + [self.close]
        ind = indicator_row(series)
        row = {
            'datetime': self.start.strftime('%Y-%m-%d %H:%M:%S'),
            'open': round(self.open, 4),
            'high': round(self.high, 4),
            'low': round(self.low, 4),
            'close': round(self.close, 4),
            'samples': self.samples,
            **ind,
        }
        if symbol_has_volume_column(symbol):
            row['volume'] = self.volume
        return row


@dataclass
class SymbolState:
    symbol: str
    day_path: str
    minute_prices: List[Tuple[datetime, float]] = field(default_factory=list)
    minute_volume: int = 0
    minute_start: Optional[datetime] = None
    closes_1m: List[float] = field(default_factory=list)
    partial_bars: Dict[int, OhlcBar] = field(default_factory=dict)
    closes_by_interval: Dict[int, List[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for interval in config.BAR_INTERVALS_MIN:
            if interval == 1:
                continue
            self.closes_by_interval[interval] = self._load_closes(interval)

    def _load_closes(self, interval_min: int) -> List[float]:
        path = config.ohlc_path(self.day_path, self.symbol, interval_min)
        if not os.path.isfile(path):
            return []
        closes: List[float] = []
        with open(path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    closes.append(float(row['close']))
                except (KeyError, TypeError, ValueError):
                    continue
        return closes

    def _load_1m_closes(self) -> None:
        self.closes_1m = self._load_closes(1)

    def ensure_day(self, day_path: str) -> None:
        if self.day_path == day_path:
            return
        self.day_path = day_path
        os.makedirs(day_path, exist_ok=True)
        self.minute_prices.clear()
        self.minute_volume = 0
        self.minute_start = None
        self.partial_bars.clear()
        self._load_1m_closes()
        for interval in config.BAR_INTERVALS_MIN:
            if interval != 1:
                self.closes_by_interval[interval] = self._load_closes(interval)

    def record_tick(self, ts: datetime, price: float) -> None:
        """Record one MQTT mid arrival — drives OHLC and raw tick log."""
        self._roll_minute(ts)
        self.minute_prices.append((ts, price))
        self._append_tick_row(ts, price)

    def record_trade_size(self, ts: datetime, size: int) -> None:
        """Add Trade.size increment to current minute volume bucket."""
        if not symbol_has_volume_column(self.symbol):
            return
        self._roll_minute(ts)
        if size > 0:
            self.minute_volume += int(size)

    def record_poll(self, ts: datetime, price: float) -> None:
        """Backward-compatible alias for record_tick."""
        self.record_tick(ts, price)

    def _roll_minute(self, ts: datetime) -> None:
        minute = ts.replace(second=0, microsecond=0)
        if self.minute_start is None:
            self.minute_start = minute
        if minute != self.minute_start:
            self._finalize_minute(self.minute_start)
            self.minute_start = minute
            self.minute_prices = []
            self.minute_volume = 0

    def _append_tick_row(self, ts: datetime, price: float) -> None:
        path = config.polls_path(self.day_path, self.symbol)
        exists = os.path.isfile(path)
        with open(path, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            if not exists:
                writer.writerow(['timestamp', 'price'])
            writer.writerow([ts.strftime('%Y-%m-%d %H:%M:%S'), round(price, 4)])

    def _append_poll_row(self, ts: datetime, price: float) -> None:
        self._append_tick_row(ts, price)

    def _finalize_minute(self, minute_start: datetime) -> None:
        if not self.minute_prices:
            return
        prices = [p for _, p in self.minute_prices]
        track_vol = symbol_has_volume_column(self.symbol)
        bar = OhlcBar(
            interval_min=1,
            start=minute_start,
            open=prices[0],
            high=max(prices),
            low=min(prices),
            close=prices[-1],
            samples=len(prices),
            volume=self.minute_volume if track_vol else 0,
            track_volume=track_vol,
        )
        self._write_bar(bar, interval_min=1)
        self.closes_1m.append(bar.close)
        self._rollup_higher_timeframes(bar)

    def flush(self) -> None:
        if self.minute_start and self.minute_prices:
            self._finalize_minute(self.minute_start)
            self.minute_prices = []
            self.minute_volume = 0
            self.minute_start = None

    def _write_bar(self, bar: OhlcBar, interval_min: int) -> None:
        if interval_min == 1:
            closes = self.closes_1m
        else:
            closes = self.closes_by_interval.setdefault(interval_min, [])
        row = bar.to_row(closes, symbol=self.symbol)
        path = config.ohlc_path(self.day_path, self.symbol, interval_min)
        exists = os.path.isfile(path)
        with open(path, 'a', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(
                f,
                fieldnames=ohlc_header(self.symbol),
                extrasaction='ignore',
            )
            if not exists:
                writer.writeheader()
            writer.writerow(row)
        closes.append(bar.close)

    def _rollup_higher_timeframes(self, bar_1m: OhlcBar) -> None:
        for interval in config.BAR_INTERVALS_MIN:
            if interval == 1:
                continue
            bucket_start = _bucket_start(bar_1m.start, interval)

            partial = self.partial_bars.get(interval)
            if partial is None or partial.start != bucket_start:
                if partial is not None and partial.samples > 0:
                    self._write_bar(partial, interval)
                partial = OhlcBar(
                    interval_min=interval,
                    start=bucket_start,
                    open=bar_1m.open,
                    high=bar_1m.high,
                    low=bar_1m.low,
                    close=bar_1m.close,
                    samples=bar_1m.samples,
                    volume=bar_1m.volume,
                    track_volume=bar_1m.track_volume,
                )
                self.partial_bars[interval] = partial
            else:
                partial.high = max(partial.high, bar_1m.high)
                partial.low = min(partial.low, bar_1m.low)
                partial.close = bar_1m.close
                partial.samples += bar_1m.samples
                partial.volume += bar_1m.volume

            if _is_bucket_complete(bar_1m.start, interval):
                if partial.samples > 0:
                    self._write_bar(partial, interval)
                self.partial_bars.pop(interval, None)
