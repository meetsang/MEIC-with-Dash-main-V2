"""Prevent short/long leg flip conflicts on the same side (legacy check_long_short)."""
from __future__ import annotations

import logging
from typing import Optional, Tuple

from common.symbols import build_tastytrade_symbol, symbols_equivalent, to_tastytrade
from blocks.stop import state as state_mod

log = logging.getLogger(__name__)

_OPEN_STATUSES = frozenset({'open', 'pending_fill'})

# Shift both legs when a new spread would flip an existing leg (same as SPX strike step).
OVERLAP_SHIFT_STEP = 5


def shift_spread_strikes(
    side: str,
    short_strike: int,
    long_strike: int,
    step: int = OVERLAP_SHIFT_STEP,
) -> Tuple[int, int]:
    """
    Move both legs to avoid leg flip: CCS $5 down, PCS $5 up (width unchanged).
    """
    side = side.upper()
    if side == 'C':
        return short_strike - step, long_strike - step
    return short_strike + step, long_strike + step


def leg_overlap_conflict(
    short_symbol: str,
    long_symbol: str,
    side: str,
    *,
    exclude_path: Optional[str] = None,
) -> Optional[str]:
    """
    Return a human-readable reason if an active trade would flip an existing leg.

    Legacy rule (MEIC-main spreadprice.check_long_short):
    - new long must not equal an existing short on the same side (P or C)
    - new short must not equal an existing long on the same side

    Scans MEIC and Manual Spread active dirs; calls vs puts are independent.
    """
    side = side.upper()
    new_short = to_tastytrade(short_symbol)
    new_long = to_tastytrade(long_symbol)

    for path in state_mod.iter_active_trade_paths():
        if exclude_path and path == exclude_path:
            continue
        try:
            st = state_mod.load_state(path)
        except (OSError, ValueError):
            continue
        if st.get('status') not in _OPEN_STATUSES:
            continue
        if str(st.get('entry', {}).get('side', '')).upper() != side:
            continue

        ex_short = to_tastytrade(st['short_leg']['symbol'])
        ex_long = to_tastytrade(st['long_leg']['symbol'])
        lot = st.get('lot', '?')

        if symbols_equivalent(new_long, ex_short):
            return (
                f'long {new_long} already open as short leg in lot {lot} '
                f'({path})'
            )
        if symbols_equivalent(new_short, ex_long):
            return (
                f'short {new_short} already open as long leg in lot {lot} '
                f'({path})'
            )
    return None


def resolve_leg_overlap(
    expiry: str,
    side: str,
    short_strike: int,
    long_strike: int,
    *,
    exclude_path: Optional[str] = None,
    max_shifts: int = 30,
    step: int = OVERLAP_SHIFT_STEP,
) -> Optional[Tuple[int, int, str, str, int]]:
    """
    Shift both legs until leg_overlap_conflict clears.

    Returns (short_strike, long_strike, short_symbol, long_symbol, shift_count)
    or None if no collision-free strike found within max_shifts.
    """
    side = side.upper()
    ss, ls = int(short_strike), int(long_strike)
    shifts = 0

    while shifts <= max_shifts:
        short_sym = build_tastytrade_symbol(expiry, side, ss)
        long_sym = build_tastytrade_symbol(expiry, side, ls)
        if leg_overlap_conflict(short_sym, long_sym, side, exclude_path=exclude_path) is None:
            return ss, ls, short_sym, long_sym, shifts

        ss, ls = shift_spread_strikes(side, ss, ls, step)
        shifts += 1
        if ss <= 0 or ls <= 0:
            return None
        if side == 'P' and ls >= ss:
            return None
        if side == 'C' and ls <= ss:
            return None

    return None
