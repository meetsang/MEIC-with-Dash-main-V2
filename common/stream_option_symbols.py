"""Registered spread option symbols from streaming/optsymbols.json."""
from __future__ import annotations

import json
import logging
from typing import List

from common.symbols import parse_canonical, to_tastytrade
from streaming import config as stream_config

log = logging.getLogger(__name__)

# Indices/equities always subscribed by streamer — not spread legs.
_INDEX_WATCH = frozenset({
    'SPX', 'VIX', 'VXN', 'QQQ', 'IWM',
    '$SPX', '.$SPX', '$VIX', '.$VIX', '$VXN', '.$VXN',
})


def load_registered_option_symbols() -> List[str]:
    """Return sorted TastyTrade option symbols from optsymbols.json (spread legs only)."""
    try:
        with open(stream_config.STREAM_SYMBOLS, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug('stream_option_symbols: cannot read %s — %s', stream_config.STREAM_SYMBOLS, exc)
        return []

    raw = data.get('SYMBOLS') or []
    seen: set[str] = set()
    out: List[str] = []
    for sym in raw:
        text = str(sym).strip()
        if not text:
            continue
        upper = text.upper().replace(' ', '')
        if upper in _INDEX_WATCH or upper in ('SPX', 'VIX', 'VXN', 'QQQ', 'IWM'):
            continue
        if parse_canonical(text) is None:
            continue
        tt = to_tastytrade(text)
        if tt in seen:
            continue
        seen.add(tt)
        out.append(tt)
    return sorted(out)
