"""Canonical market-data watch list, DXLink mapping, and sidecar ladder flags."""
from __future__ import annotations

import os
from datetime import time
from typing import FrozenSet, Optional, Tuple

FUTURES_WATCH_SYMBOLS: Tuple[str, ...] = (
    'ES',
    'MES',
)

# Base symbols for core index, ETF, and futures tracking (single source of truth)
BASE_WATCH_SYMBOLS: Tuple[str, ...] = (
    'SPX',
    'VIX',
    'VIX1D',
    'VXN',
    'QQQ',
    'IWM',
    'TLT',
    'GLD',
    'ES',
    'MES',
)

def get_drma_stock_watch_symbols(root: Optional[str] = None) -> Tuple[str, ...]:
    """Retrieve currently active/enabled DRMA stock symbols directly from the watchlist file (page)."""
    try:
        from strategies.drma_stocks.watchlist import get_active_symbols
        # Returns only symbols currently enabled in config/drma_stocks_watchlist.json
        return tuple(get_active_symbols(root))
    except Exception:
        return ()


def get_all_watch_symbols(root: Optional[str] = None) -> Tuple[str, ...]:
    """Dynamic union of core symbols and whatever is currently active on the DRMA stocks page."""
    return tuple(dict.fromkeys(list(BASE_WATCH_SYMBOLS) + list(get_drma_stock_watch_symbols(root))))


class _DynamicWatchSymbols(tuple):
    """Tuple-compatible view that reflects dynamic watchlist additions/deletions from the dashboard page."""
    def __iter__(self):
        return iter(get_all_watch_symbols())
    def __contains__(self, item):
        return item in BASE_WATCH_SYMBOLS or item in get_drma_stock_watch_symbols()
    def __len__(self):
        return len(get_all_watch_symbols())
    def __repr__(self):
        return repr(get_all_watch_symbols())


WATCH_SYMBOLS: Tuple[str, ...] = _DynamicWatchSymbols(BASE_WATCH_SYMBOLS)

SPX_SYMBOL = 'SPX'
SPX_NO_VOLUME: FrozenSet[str] = frozenset({SPX_SYMBOL})
# CBOE cash indices stream ticks on DXLink Trade channel (last price) without bid/ask Quote mids.
INDEX_TRADE_PRICE_SYMBOLS: FrozenSet[str] = frozenset({'SPX', 'VIX', 'VIX1D', 'VXN'})

# MQTT topic suffix for cumulative day volume (float string); not consumed by stop monitor.
VOLUME_TOPIC_SUFFIX = '__VOL'
# Per-trade size increment for OHLCV bar volume (float string).
TRADE_SIZE_TOPIC_SUFFIX = '__TSIZE'

# DXLink Quote/Trade subscribe names (event_symbol on wire).
# On TastyTrade DXLink, CBOE indices are 'SPX', 'VIX', 'VIX1D', 'VXN' (no '$' prefix).
DXLINK_QUOTE_SYMBOL: dict[str, str] = {
    'SPX': 'SPX',
    'VIX': 'VIX',
    'VIX1D': 'VIX1D',
    'VXN': 'VXN',
    'QQQ': 'QQQ',
    'IWM': 'IWM',
    'TLT': 'TLT',
    'GLD': 'GLD',
}

# MQTT canonical topic names (aliases from DXLink or Schwab event symbols).
DXLINK_TO_MQTT: dict[str, str] = {
    'SPX': 'SPX',
    '$SPX': 'SPX',
    '.$SPX': 'SPX',
    'VIX': 'VIX',
    '$VIX': 'VIX',
    '.$VIX': 'VIX',
    'VIX1D': 'VIX1D',
    '$VIX1D': 'VIX1D',
    '.$VIX1D': 'VIX1D',
    'VXN': 'VXN',
    '$VXN': 'VXN',
    '.$VXN': 'VXN',
    'QQQ': 'QQQ',
    'IWM': 'IWM',
    'TLT': 'TLT',
    'GLD': 'GLD',
    'ES': 'ES',
    '/ES': 'ES',
    '/ES:XCME': 'ES',
    'MES': 'MES',
    '/MES': 'MES',
    '/MES:XCME': 'MES',
}

# Trade channel for OHLCV (all watch symbols except SPX index and futures,
# whose front-month DXLink contract symbols are resolved dynamically).
TRADE_WATCH_SYMBOLS: Tuple[str, ...] = tuple(
    s for s in WATCH_SYMBOLS if s not in SPX_NO_VOLUME and s not in FUTURES_WATCH_SYMBOLS
)

# --- Sidecar SPX ladder defaults (ON unless env disables or LOW_MEMORY_MODE) ---
MARKET_DATA_OPTION_COLLECTION_ENABLED = True
SPX_LADDER_ENABLED = True
SPX_LADDER_QUOTES_ENABLED = True
SPX_LADDER_VOLUME_ENABLED = False


def _get_spx_ladder_max_symbols() -> int:
    raw = os.environ.get('SPX_LADDER_MAX_ACTIVE_SYMBOLS')
    if raw is not None:
        try:
            return max(0, int(raw.strip()))
        except ValueError:
            pass
    if os.environ.get('LOW_MEMORY_MODE', '').strip().lower() in ('1', 'true', 'yes'):
        return 50
    return 500


SPX_LADDER_MAX_ACTIVE_SYMBOLS = _get_spx_ladder_max_symbols()
SPX_LADDER_REFRESH_SEC = 60

# Regular session window for ladder refresh (US/Central, naive).
LADDER_SESSION_OPEN = time(8, 30)
LADDER_SESSION_CLOSE = time(15, 0)

_SIDEcar_disabled_logged = False


def sidecar_option_collection_enabled() -> bool:
    """True unless MEIC_SIDE_OPTION_COLLECTION explicitly disables sidecar or LOW_MEMORY_MODE is set."""
    raw = os.environ.get('MEIC_SIDE_OPTION_COLLECTION', '').strip().lower()
    if raw in ('0', 'false', 'no', 'off'):
        return False
    if raw in ('1', 'true', 'yes', 'on'):
        return True
    if os.environ.get('LOW_MEMORY_MODE', '').strip().lower() in ('1', 'true', 'yes'):
        return False
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
    combined = list(dict.fromkeys(list(TRADE_WATCH_SYMBOLS) + list(get_drma_stock_watch_symbols())))
    return [dxlink_quote_symbol(s) for s in combined]


def mqtt_topic_from_dxlink(event_symbol: str) -> str:
    sym = (event_symbol or '').strip()
    if sym in DXLINK_TO_MQTT:
        return DXLINK_TO_MQTT[sym]
    upper = sym.upper()
    if upper.startswith(('/ES', 'ES')):
        return 'ES'
    if upper.startswith(('/MES', 'MES')):
        return 'MES'
    if upper.startswith(('/NQ', 'NQ')):
        return 'NQ'
    if upper.startswith(('/MNQ', 'MNQ')):
        return 'MNQ'
    return sym


def canonical_watch_symbol(topic_or_dxlink: str) -> Optional[str]:
    mqtt = mqtt_topic_from_dxlink(topic_or_dxlink)
    if mqtt in WATCH_SYMBOLS or mqtt in get_drma_stock_watch_symbols():
        return mqtt
    return None


def symbol_has_volume_column(symbol: str) -> bool:
    return symbol not in SPX_NO_VOLUME


def is_ladder_session(now) -> bool:
    """True during 8:30–15:00 CT on a naive central datetime."""
    t = now.time()
    return LADDER_SESSION_OPEN <= t < LADDER_SESSION_CLOSE
