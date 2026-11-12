"""Streamer subscribe-set builder — watch, trade legs, sidecar ladder."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional, Set, Tuple

from common.market_watch import (
    FUTURES_WATCH_SYMBOLS,
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
    from common.data_utils import load_json_safe
    data = load_json_safe(config.STREAM_SYMBOLS)
    if not isinstance(data, dict):
        return set()
    raw = data.get('SYMBOLS') or []

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
    return {dxlink_quote_symbol(s) for s in WATCH_SYMBOLS if s not in FUTURES_WATCH_SYMBOLS}


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


def _active_trade_quote_symbols() -> Set[str]:
    """Scan all active strategy directories for open/working trades and extract option symbols dynamically.

    Guarantees 0DTE, 1DTE, or multi-DTE trades are always automatically streamed by DXLink
    even across launcher restarts or if optsymbols.json was not yet written.
    """
    out: Set[str] = set()
    try:
        from common import trades_layout
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        for act_dir in trades_layout.all_active_dirs(root):
            if not os.path.isdir(act_dir):
                continue
            for fname in os.listdir(act_dir):
                if not fname.endswith('.json') or fname.startswith('.'):
                    continue
                fpath = os.path.join(act_dir, fname)
                try:
                    with open(fpath, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                    st = str(data.get('status', 'open')).lower()
                    if st not in ('open', 'closing', 'pending', 'working', 'pending_fill'):
                        continue

                    syms_to_add = []
                    s_sym = (data.get('short_leg') or {}).get('symbol')
                    l_sym = (data.get('long_leg') or {}).get('symbol')
                    if s_sym:
                        syms_to_add.append(s_sym)
                    if l_sym:
                        syms_to_add.append(l_sym)
                    if data.get('body_symbol'):
                        syms_to_add.append(data['body_symbol'])
                    for st_item in data.get('structures') or []:
                        for leg in st_item.get('legs') or []:
                            if leg.get('symbol'):
                                syms_to_add.append(leg['symbol'])
                    spec = data.get('spec') or {}
                    if spec.get('stream_symbol'):
                        syms_to_add.append(spec['stream_symbol'])
                    elif spec.get('tasty_symbol'):
                        syms_to_add.append(spec['tasty_symbol'])

                    for s in syms_to_add:
                        text = str(s).strip()
                        if not text:
                            continue
                        dx = to_tastytrade(text) if not text.startswith('.') else text
                        if dx:
                            out.add(dx)
                except Exception:
                    pass
    except Exception as exc:
        log.debug('Error scanning active trade quote symbols: %s', exc)
    return out


def build_quote_subscribe_set() -> Tuple[Set[str], dict]:
    """Union WATCH ∪ trade legs ∪ active trade legs ∪ capped ladder (deduped). Trade legs always included."""
    trade_legs = _read_optsymbols() | _active_trade_quote_symbols()
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
