"""Periodic snapshot of sidecar SPX ladder mids (separate from options_quotes.csv)."""
from __future__ import annotations

import csv
import logging
import os
import time
from datetime import datetime
from typing import Dict, Optional

from common.market_watch import (
    SPX_LADDER_QUOTES_ENABLED,
    SPX_LADDER_REFRESH_SEC,
    SPX_LADDER_VOLUME_ENABLED,
    is_ladder_session,
    sidecar_option_collection_enabled,
    log_sidecar_disabled_once,
)
from common.stream_ladder_symbols import load_ladder_option_symbols
from market_data import config
from market_data.spx_ladder import parse_ladder_symbol

log = logging.getLogger(__name__)

_CSV_HEADER_MID = ('snapshot_ts', 'strike', 'side', 'symbol', 'mid')
_CSV_HEADER_VOL = ('snapshot_ts', 'strike', 'side', 'symbol', 'mid', 'volume')


class SpxLadderSnapshotWriter:
    def __init__(self, cache):
        self._cache = cache
        self._last_snapshot_mono = 0.0
        self._day_path: Optional[str] = None

    def maybe_write(self, now: datetime, *, day_path: str) -> bool:
        if not sidecar_option_collection_enabled():
            log_sidecar_disabled_once(log)
            return False
        if not SPX_LADDER_QUOTES_ENABLED:
            return False
        if not is_ladder_session(now):
            return False
        if self._last_snapshot_mono and (
            time.monotonic() - self._last_snapshot_mono
        ) < SPX_LADDER_REFRESH_SEC:
            return False

        symbols = load_ladder_option_symbols()
        if not symbols:
            return False

        quotes: Dict[str, float] = {}
        volumes: Dict[str, int] = {}
        for sym in symbols:
            mid = self._cache.get_market_mid(sym)
            if mid is not None:
                quotes[sym] = round(float(mid), 4)
            if SPX_LADDER_VOLUME_ENABLED:
                vol = self._cache.get_day_volume(sym)
                if vol is not None:
                    volumes[sym] = int(vol)

        if not quotes:
            return False

        self._day_path = day_path
        os.makedirs(day_path, exist_ok=True)
        path = config.spx_ladder_quotes_path(day_path)
        write_header = not os.path.isfile(path)
        ts = now.strftime('%Y-%m-%d %H:%M:%S')
        header = _CSV_HEADER_VOL if SPX_LADDER_VOLUME_ENABLED else _CSV_HEADER_MID

        with open(path, 'a', encoding='utf-8', newline='') as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(header)
            for sym in sorted(quotes):
                parsed = parse_ladder_symbol(sym)
                strike, side = parsed if parsed else ('', '')
                row = [ts, strike, side, sym, quotes[sym]]
                if SPX_LADDER_VOLUME_ENABLED:
                    row.append(volumes.get(sym, 0))
                writer.writerow(row)

        self._last_snapshot_mono = time.monotonic()
        log.info(
            'SPX ladder snapshot — %d/%d symbols @ %s → %s',
            len(quotes),
            len(symbols),
            ts,
            os.path.basename(path),
        )
        return True
