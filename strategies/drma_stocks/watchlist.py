"""Watchlist persistence and management for DRMA Stocks strategy."""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

DEFAULT_TICKERS = [
    {"symbol": "NVDA", "name": "NVIDIA Corporation", "enabled": True},
    {"symbol": "MU", "name": "Micron Technology Inc", "enabled": True},
    {"symbol": "AMZN", "name": "Amazon.com Inc", "enabled": True},
    {"symbol": "SPCX", "name": "CrossingBridge Pre-Merger SPAC ETF", "enabled": True},
    {"symbol": "MSTR", "name": "MicroStrategy Inc", "enabled": True},
]
DEFAULT_WATCHLIST = DEFAULT_TICKERS


def _get_watchlist_path(root: Optional[str] = None) -> str:
    base = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    return os.path.join(base, 'config', 'drma_stocks_watchlist.json')


def load_watchlist(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Load tracked stock list from JSON file or return defaults."""
    path = _get_watchlist_path(root)
    if not os.path.isfile(path):
        save_watchlist(DEFAULT_TICKERS, root)
        return list(DEFAULT_TICKERS)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
            tickers = data.get('tickers') if isinstance(data, dict) else data
            if isinstance(tickers, list) and len(tickers) > 0:
                return tickers
    except Exception as exc:
        log.warning("Could not read watchlist %s: %s; using defaults", path, exc)
    return list(DEFAULT_TICKERS)


def save_watchlist(tickers: List[Dict[str, Any]], root: Optional[str] = None) -> bool:
    """Save watchlist back to JSON file."""
    path = _get_watchlist_path(root)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump({'tickers': tickers}, f, indent=2)
        return True
    except Exception as exc:
        log.error("Failed to write watchlist %s: %s", path, exc)
        return False


def get_active_symbols(root: Optional[str] = None) -> List[str]:
    """Return list of enabled stock symbols (e.g. ['NVDA', 'MU', ...])."""
    tickers = load_watchlist(root)
    return [
        t['symbol'].strip().upper()
        for t in tickers
        if t.get('enabled', True) and t.get('symbol')
    ]


def add_ticker(symbol: str, name: str = '', root: Optional[str] = None) -> Tuple[bool, str]:
    """Add a new ticker to the watchlist."""
    clean_sym = re.sub(r'[^A-Za-z0-9]', '', (symbol or '')).strip().upper()
    if not clean_sym:
        return False, "Invalid ticker symbol"
    tickers = load_watchlist(root)
    for t in tickers:
        if t.get('symbol', '').upper() == clean_sym:
            if t.get('enabled', True):
                return False, f"{clean_sym} already in watchlist"
            t['enabled'] = True
            save_watchlist(tickers, root)
            return True, f"{clean_sym} already exists, re-enabled."
    tickers.append({
        'symbol': clean_sym,
        'name': name or f"{clean_sym} Equity",
        'enabled': True,
    })
    ok = save_watchlist(tickers, root)
    return ok, f"Added {clean_sym} to watchlist" if ok else "Failed to save"


def remove_ticker(symbol: str, root: Optional[str] = None) -> Tuple[bool, str]:
    """Remove a ticker from the watchlist."""
    clean_sym = symbol.strip().upper()
    tickers = load_watchlist(root)
    before = len(tickers)
    tickers = [t for t in tickers if t.get('symbol', '').upper() != clean_sym]
    if len(tickers) == before:
        return False, f"Ticker {clean_sym} not found in watchlist"
    ok = save_watchlist(tickers, root)
    return ok, f"Removed {clean_sym} from watchlist" if ok else "Failed to save"


def toggle_ticker(symbol: str, enabled: Optional[bool] = None, root: Optional[str] = None) -> Tuple[bool, str]:
    """Toggle or set active status for a ticker."""
    clean_sym = symbol.strip().upper()
    tickers = load_watchlist(root)
    found = False
    for t in tickers:
        if t.get('symbol', '').upper() == clean_sym:
            found = True
            t['enabled'] = (not t.get('enabled', True)) if enabled is None else bool(enabled)
            break
    if not found:
        return False, f"Ticker {clean_sym} not found"
    ok = save_watchlist(tickers, root)
    return ok, f"Updated {clean_sym} status" if ok else "Failed to save"
