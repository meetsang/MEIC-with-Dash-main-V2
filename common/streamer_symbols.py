"""Register spread option symbols for the MQTT streamer (optsymbols.json)."""
from __future__ import annotations

import logging
from typing import Optional

from common.symbols import build_schwab_symbol, parse_canonical

log = logging.getLogger(__name__)


def schwab_symbols_from_state(state: dict) -> tuple[str, str]:
    """Build Schwab-format symbols from stop_monitor JSON state."""
    parsed = parse_canonical(state['short_leg']['symbol'])
    if not parsed:
        raise ValueError(f"Cannot parse short symbol: {state['short_leg']['symbol']}")
    expiry, opt_type, _ = parsed
    short_s = state['short_leg']['strike']
    long_s = state['long_leg']['strike']
    return (
        build_schwab_symbol(expiry, opt_type, short_s),
        build_schwab_symbol(expiry, opt_type, long_s),
    )


def register_spread_symbols(state: dict, lot: str, logger: Optional[logging.Logger] = None) -> bool:
    """Add spread legs to streaming/optsymbols.json so publish_tastytrade picks them up."""
    slog = logger or log
    try:
        from meic0dte.app import utilities as util

        short_sym, long_sym = schwab_symbols_from_state(state)
        return util.update_options_symbols([short_sym, long_sym], lot, slog)
    except Exception as exc:
        slog.error('register_spread_symbols failed: %s', exc)
        return False
