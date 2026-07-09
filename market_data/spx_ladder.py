"""SPX 0DTE options ladder — strike grid and symbol JSON writer."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import List, Optional, Tuple

from common.market_watch import (
    SPX_LADDER_MAX_ACTIVE_SYMBOLS,
    SPX_LADDER_REFRESH_SEC,
    is_ladder_session,
    sidecar_option_collection_enabled,
    log_sidecar_disabled_once,
)
from common.symbols import build_tastytrade_symbol
from meic0dte.app.utilities import central_date, central_now

log = logging.getLogger(__name__)

LADDER_SYMBOL_FILE = os.path.join(
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..')),
    'streaming',
    'spx_ladder_symbols.json',
)


def anchor_strike(spx: float) -> int:
    return int(round(float(spx) / 5) * 5)


def strikes_for_spx(spx: float) -> List[int]:
    """50 below + 50 at/above anchor (100 strikes)."""
    anchor = anchor_strike(spx)
    below = [anchor - 5 * i for i in range(1, 51)]
    above = [anchor + 5 * i for i in range(0, 50)]
    return sorted(set(below + above))


def ladder_option_symbols(spx: float, expiry_yymmdd: str) -> List[str]:
    """Calls + puts for each strike → up to 200 TastyTrade symbols."""
    out: List[str] = []
    for strike in strikes_for_spx(spx):
        out.append(build_tastytrade_symbol(expiry_yymmdd, 'C', strike))
        out.append(build_tastytrade_symbol(expiry_yymmdd, 'P', strike))
    return out


def parse_ladder_symbol(symbol: str) -> Optional[Tuple[int, str]]:
    """Return (strike, 'C'|'P') from .SPXW... symbol."""
    from common.symbols import parse_canonical

    parsed = parse_canonical(symbol)
    if not parsed:
        return None
    _expiry, opt_type, strike = parsed
    return strike, opt_type


class SpxLadderWriter:
    """Refresh spx_ladder_symbols.json from live SPX MQTT mid."""

    def __init__(self, *, max_symbols: int = SPX_LADDER_MAX_ACTIVE_SYMBOLS):
        self._last_refresh_mono = 0.0
        self._max_symbols = max_symbols

    def maybe_refresh(self, cache, now: Optional[datetime] = None) -> bool:
        if not sidecar_option_collection_enabled():
            log_sidecar_disabled_once(log)
            return False

        from common.market_watch import SPX_LADDER_ENABLED

        if not SPX_LADDER_ENABLED:
            return False

        now = now or central_now()
        if not is_ladder_session(now):
            return False

        import time

        if self._last_refresh_mono and (
            time.monotonic() - self._last_refresh_mono
        ) < SPX_LADDER_REFRESH_SEC:
            return False

        spx = cache.get_market_mid('SPX')
        if spx is None:
            spx = cache.get('SPX')
        if spx is None or float(spx) <= 0:
            return False

        expiry = central_date().strftime('%y%m%d')
        symbols = ladder_option_symbols(float(spx), expiry)
        if len(symbols) > self._max_symbols:
            log.critical(
                'SPX ladder symbol cap hit (%d > %d) — truncating ladder only',
                len(symbols),
                self._max_symbols,
            )
            symbols = symbols[: self._max_symbols]

        payload = {
            'updated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'anchor_strike': anchor_strike(float(spx)),
            'spx_ref': round(float(spx), 4),
            'expiry_yymmdd': expiry,
            'SYMBOLS': symbols,
        }
        os.makedirs(os.path.dirname(LADDER_SYMBOL_FILE), exist_ok=True)
        tmp = f'{LADDER_SYMBOL_FILE}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, LADDER_SYMBOL_FILE)

        self._last_refresh_mono = time.monotonic()
        log.info(
            'SPX ladder refreshed — anchor=%s spx=%.2f symbols=%d',
            payload['anchor_strike'],
            payload['spx_ref'],
            len(symbols),
        )
        return True
