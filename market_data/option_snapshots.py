"""Periodic snapshot of MQTT option mids — no OHLC aggregation."""
from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

from common.mqtt_prices import MqttPriceCache
from common.stream_option_symbols import load_registered_option_symbols
from market_data import config

log = logging.getLogger(__name__)

_CSV_HEADER = ('snapshot_ts', 'symbol', 'mid')


class OptionQuoteSnapshotWriter:
    """Append all registered option mids to one daily CSV every N minutes."""

    def __init__(self, cache: MqttPriceCache):
        self._cache = cache
        self._last_snapshot_mono = 0.0
        self._day_path: Optional[str] = None

    def _ensure_day(self, day_path: str) -> None:
        if self._day_path != day_path:
            self._day_path = day_path
            os.makedirs(day_path, exist_ok=True)

    def maybe_write(self, now: datetime, *, day_path: str) -> bool:
        """Write one snapshot block if interval elapsed. Returns True if file updated."""
        if self._last_snapshot_mono and (
            time.monotonic() - self._last_snapshot_mono
        ) < config.OPTION_SNAPSHOT_INTERVAL_SEC:
            return False

        symbols = load_registered_option_symbols()
        if not symbols:
            return False

        quotes: Dict[str, float] = {}
        for sym in symbols:
            mid = self._cache.get_market_mid(sym)
            if mid is not None:
                quotes[sym] = round(float(mid), 4)

        if not quotes:
            return False

        self._ensure_day(day_path)
        path = config.options_quotes_path(day_path)
        write_header = not os.path.isfile(path)
        ts = now.strftime('%Y-%m-%d %H:%M:%S')

        with open(path, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(_CSV_HEADER)
            for sym in sorted(quotes):
                writer.writerow([ts, sym, quotes[sym]])

        self._last_snapshot_mono = time.monotonic()
        log.info(
            'Option quote snapshot — %d/%d symbols @ %s → %s',
            len(quotes),
            len(symbols),
            ts,
            os.path.basename(path),
        )
        return True
