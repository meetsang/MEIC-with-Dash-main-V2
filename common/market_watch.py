"""Canonical market-data watch list, DXLink mapping, and sidecar ladder flags."""
from __future__ import annotations

import os
from datetime import time
from typing import FrozenSet, Optional, Tuple

# --- Watch universe (single source of truth) ---
WATCH_SYMBOLS: Tuple[str, ...] = (
    'SPX',
    'VIX',
    'VXN',
    'QQQ',
    'IWM',
    'TLT',
    'GLD',
)

SPX_SYMBOL = 'SPX'
SPX_NO_VOLUME: FrozenSet[str] = frozenset({SPX_SYMBOL})

# MQTT topic suffix for cumulative day volume (float string); not consumed by stop monitor.
VOLUME_TOPIC_SUFFIX = '__VOL'
# Per-trade size increment for OHLCV bar volume (float string).
TRADE_SIZE_TOPIC_SUFFIX = '__TSIZE'

# DXLink Quote subscribe names (event_symbol on wire).
DXLINK_QUOTE_SYMBOL: dict[str, str] = {
    'SPX': 'SPX',
    'VIX': '$VIX',
    'VXN': '$VXN',
    'QQQ': 'QQQ',
    'IWM': 'IWM',
    'TLT': 'TLT',
    'GLD': 'GLD',
}

# MQTT canonical topic names (aliases from DXLink event symbols).
DXLINK_TO_MQTT: dict[str, str] = {
    'SPX': 'SPX',
    '$SPX': 'SPX',
    '.$SPX': 'SPX',
    'VIX': 'VIX',
    '$VIX': 'VIX',
    '.$VIX': 'VIX',
    'VXN': 'VXN',
    '$VXN': 'VXN',
    '.$VXN': 'VXN',
    'QQQ': 'QQQ',
    'IWM': 'IWM',
    'TLT': 'TLT',
    'GLD': 'GLD',
}

# Trade channel for OHLCV (all watch symbols except SPX index).
TRADE_WATCH_SYMBOLS: Tuple[str, ...] = tuple(
    s for s in WATCH_SYMBOLS if s not in SPX_NO_VOLUME
)

# --- Sidecar SPX ladder defaults (ON unless env disables) ---
MARKET_DATA_OPTION_COLLECTION_ENABLED = True
SPX_LADDER_ENABLED = True
SPX_LADDER_QUOTES_ENABLED = True
SPX_LADDER_VOLUME_ENABLED = False
SPX_LADDER_MAX_ACTIVE_SYMBOLS = 500
SPX_LADDER_REFRESH_SEC = 60

# Regular session window for ladder refresh (US/Central, naive).
LADDER_SESSION_OPEN = time(8, 30)
LADDER_SESSION_CLOSE = time(15, 0)

_SIDEcar_disabled_logged = False


def sidecar_option_collection_enabled() -> bool:
    """True unless MEIC_SIDE_OPTION_COLLECTION explicitly disables sidecar."""
    raw = os.environ.get('MEIC_SIDE_OPTION_COLLECTION')
    if raw is not None:
        return raw.strip().lower() not in ('0', 'false', 'no', 'off')
    return bool(MARKET_DATA_OPTION_COLLECTION_ENABLED)


def log_sidecar_disabled_once(logger) -> None:
    global _SIDEcar_disabled_logged
    if _SIDEcar_disabled_logged:
        return
    _SIDEcar_disabled_logged = True
    logger.info('sidecar_option_collection_disabled')


def dxlink_quote_symbol(canonical: str) -> str:
    return DXLINK_QUOTE_SYMBOL.get(canonical, canonical)


def dxlink_trade_symbols() -> list[str]:
    return [dxlink_quote_symbol(s) for s in TRADE_WATCH_SYMBOLS]


def mqtt_topic_from_dxlink(event_symbol: str) -> str:
    sym = (event_symbol or '').strip()
    return DXLINK_TO_MQTT.get(sym, sym)


def canonical_watch_symbol(topic_or_dxlink: str) -> Optional[str]:
    mqtt = mqtt_topic_from_dxlink(topic_or_dxlink)
    if mqtt in WATCH_SYMBOLS:
        return mqtt
    return None


def symbol_has_volume_column(symbol: str) -> bool:
    return symbol not in SPX_NO_VOLUME


def is_ladder_session(now) -> bool:
    """True during 8:30–15:00 CT on a naive central datetime."""
    t = now.time()
    return LADDER_SESSION_OPEN <= t < LADDER_SESSION_CLOSE
