"""Load sidecar SPX ladder symbols from streaming/spx_ladder_symbols.json."""
from __future__ import annotations

import json
import logging
import os
from typing import List

from common.market_watch import sidecar_option_collection_enabled
from common.symbols import to_tastytrade

log = logging.getLogger(__name__)

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LADDER_SYMBOL_FILE = os.path.join(_ROOT, 'streaming', 'spx_ladder_symbols.json')


def load_ladder_option_symbols() -> List[str]:
    if not sidecar_option_collection_enabled():
        return []
    try:
        with open(LADDER_SYMBOL_FILE, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug('stream_ladder_symbols: cannot read %s — %s', LADDER_SYMBOL_FILE, exc)
        return []

    raw = data.get('SYMBOLS') or []
    seen: set[str] = set()
    out: List[str] = []
    for sym in raw:
        text = str(sym).strip()
        if not text:
            continue
        tt = to_tastytrade(text) if not text.startswith('.') else text
        if tt in seen:
            continue
        seen.add(tt)
        out.append(tt)
    return sorted(out)
