"""Streamer subscribe-set builder — watch, trade legs, sidecar ladder."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional, Set, Tuple

from common.market_watch import (
    SPX_LADDER_MAX_ACTIVE_SYMBOLS,
    SPX_LADDER_VOLUME_ENABLED,
    SPX_SYMBOL,
    WATCH_SYMBOLS,
    dxlink_quote_symbol,
    dxlink_trade_symbols,
    log_sidecar_disabled_once,
    sidecar_option_collection_enabled,
)
from common.stream_ladder_symbols import load_ladder_option_symbols
from common.symbols import to_tastytrade
from streaming import config

log = logging.getLogger(__name__)

BACKOFF_BASE_SEC = 2.0
BACKOFF_MAX_SEC = 300.0


def _read_optsymbols() -> Set[str]:
    try:
        with open(config.STREAM_SYMBOLS, 'r', encoding='utf-8') as f:
            data = json.load(f)
        raw = data.get('SYMBOLS') or []
    except Exception as exc:
        log.info('ERROR reading optsymbols.json: %s', exc)
        return set()
    out: Set[str] = set()
    for sym in raw:
        text = str(sym).strip()
        if not text:
            continue
        if text == 'SPX':
            out.add('SPX')
            continue
        out.add(to_tastytrade(text) if not text.startswith('.') else text)
    return out


def _watch_quote_symbols() -> Set[str]:
    return {dxlink_quote_symbol(s) for s in WATCH_SYMBOLS}


def _capped_ladder_symbols(trade_legs: Set[str]) -> Tuple[Set[str], bool]:
    if not sidecar_option_collection_enabled():
        return set(), False
    ladder = load_ladder_option_symbols()
    if not ladder:
        return set(), False
    cap_hit = False
    out: Set[str] = set()
    for sym in ladder:
        if sym in trade_legs:
            continue
        if len(out) >= SPX_LADDER_MAX_ACTIVE_SYMBOLS:
            cap_hit = True
            break
        out.add(sym)
    if cap_hit:
        log.critical(
            'SPX ladder subscribe cap hit (%d) — no new ladder symbols; trade legs unaffected',
            SPX_LADDER_MAX_ACTIVE_SYMBOLS,
        )
    return out, cap_hit


def build_quote_subscribe_set() -> Tuple[Set[str], dict]:
    """Union WATCH ∪ trade legs ∪ capped ladder (deduped). Trade legs always included."""
    trade_legs = _read_optsymbols()
    watch = _watch_quote_symbols()
    ladder, cap_hit = _capped_ladder_symbols(trade_legs)
    quote_set = set(watch) | set(trade_legs) | set(ladder)
    meta = {
        'trade_leg_count': len(trade_legs),
        'watch_count': len(watch),
        'ladder_count': len(ladder),
        'total': len(quote_set),
        'ladder_cap_hit': cap_hit,
        'ladder_enabled': sidecar_option_collection_enabled(),
    }
    return quote_set, meta


def build_trade_subscribe_set(quote_set: Set[str]) -> Set[str]:
    """DXLink Trade symbols for watch OHLCV + SPX last-sale + optional ladder volume."""
    trades = set(dxlink_trade_symbols())
    # SPX is excluded from dxlink_trade_symbols() (SPX_NO_VOLUME for OHLCV bars) but
    # last-sale Trade events improve dashboard/navbar accuracy vs quote-only mids.
    trades.add(dxlink_quote_symbol(SPX_SYMBOL))
    if SPX_LADDER_VOLUME_ENABLED and sidecar_option_collection_enabled():
        for sym in quote_set:
            if sym.startswith('.SPXW'):
                trades.add(sym)
    return trades


class LadderSubscribeGuard:
    """Quarantine bad ladder symbols; exponential backoff on subscribe errors."""

    def __init__(self):
        self._quarantined: Set[str] = set()
        self._retry_after: dict[str, float] = {}
        self._fail_counts: dict[str, int] = {}
        self.last_error: Optional[str] = None

    def filter_subscribe(self, symbols: Set[str]) -> Set[str]:
        now = time.time()
        out: Set[str] = set()
        for sym in symbols:
            if sym in self._quarantined:
                continue
            if now < self._retry_after.get(sym, 0.0):
                continue
            out.add(sym)
        return out

    def mark_success(self, symbols: Set[str]) -> None:
        for sym in symbols:
            self._fail_counts.pop(sym, None)
            self._retry_after.pop(sym, None)

    def mark_failed(self, symbols: Set[str], error: str) -> None:
        self.last_error = error
        now = time.time()
        for sym in symbols:
            if not sym.startswith('.SPXW'):
                continue
            count = self._fail_counts.get(sym, 0) + 1
            self._fail_counts[sym] = count
            if count >= 3:
                self._quarantined.add(sym)
                log.warning('Ladder symbol quarantined for session: %s (%s)', sym, error)
            else:
                delay = min(BACKOFF_MAX_SEC, BACKOFF_BASE_SEC * (2 ** (count - 1)))
                self._retry_after[sym] = now + delay

    @property
    def quarantined_count(self) -> int:
        return len(self._quarantined)
