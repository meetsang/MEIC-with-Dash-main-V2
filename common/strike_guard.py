"""Prevent short/long leg flip conflicts on the same side (legacy check_long_short)."""
from __future__ import annotations

import logging
import os
from typing import Optional, Tuple

from common.symbols import build_tastytrade_symbol, symbols_equivalent, to_tastytrade
from blocks.stop import state as state_mod

log = logging.getLogger(__name__)

_OPEN_STATUSES = frozenset({'open', 'pending_fill'})
_CLOSED_STATUSES = frozenset({
    'closed', 'cancelled', 'canceled', 'expired', 'settled', 'rejected', 'killed',
})

# Shift both legs when a new spread would flip an existing leg (same as SPX strike step).
OVERLAP_SHIFT_STEP = 5


def shift_spread_strikes(
    side: str,
    short_strike: int,
    long_strike: int,
    step: int = OVERLAP_SHIFT_STEP,
    direction: str = 'default',
) -> Tuple[int, int]:
    """
    Move both legs to avoid leg flip.
    direction='default': CCS $5 down (toward ATM), PCS $5 up (toward ATM).
    direction='reverse': CCS $5 up (further OTM), PCS $5 down (further OTM).
    """
    side = side.upper()
    if direction == 'reverse':
        if side == 'C':
            return short_strike + step, long_strike + step
        return short_strike - step, long_strike - step

    if side == 'C':
        return short_strike - step, long_strike - step
    return short_strike + step, long_strike + step


def is_trade_paper(st: dict, path: Optional[str] = None) -> bool:
    """Return True if trade is paper, False if live."""
    if 'paper' in st and st['paper'] is not None:
        return bool(st['paper'])
    open_ord_id = str(st.get('open_order_id') or '')
    if open_ord_id.startswith('journal-'):
        return True
    strat = (st.get('entry') or {}).get('strategy') or st.get('strategy')
    if not strat and path:
        parent = os.path.basename(os.path.dirname(path))
        if parent:
            strat = parent
    if strat:
        try:
            from common.broker_factory import is_strategy_paper
            return is_strategy_paper(strat, default=False)
        except Exception:
            pass
    return False


def leg_overlap_conflict(
    short_symbol: str,
    long_symbol: str,
    side: str,
    *,
    exclude_path: Optional[str] = None,
    allow_same_spread: bool = True,
    paper: Optional[bool] = None,
) -> Optional[str]:
    """
    Return a human-readable reason if an active trade would flip an existing leg.
    Duplicate vertical spreads (same short and long strikes in the same roles)
    are permitted by default unless allow_same_spread=False is explicitly specified.

    When paper is specified (True or False), only active trades with matching
    execution mode (paper vs live) are checked. A paper trade never conflicts with
    a live trade, and a live trade never conflicts with a paper trade.

    Legacy flip conflict rule:
    - new long must not equal an existing short on the same side (P or C)
      (e.g., if open PCS is 7700/7675, a new PCS 7725/7700 has debit 7700 reversing credit 7700)
    - new short must not equal an existing long on the same side
      (e.g., if open PCS is 7700/7675, a new PCS 7675/7650 has credit 7675 reversing debit 7675)

    Closed or terminal trades (status closed/cancelled/expired/settled, close block present,
    zero holding quantity, or past expiry date) are strictly ignored.
    """
    side = side.upper()
    new_short = to_tastytrade(short_symbol)
    new_long = to_tastytrade(long_symbol)

    for path in state_mod.iter_active_trade_paths():
        if exclude_path and os.path.normpath(path) == os.path.normpath(exclude_path):
            continue
        try:
            st = state_mod.load_state(path)
        except (OSError, ValueError):
            continue

        # 0. Paper vs Live isolation:
        # Paper trades only block paper trades; Live trades only block live trades.
        if paper is not None:
            trade_is_paper = is_trade_paper(st, path)
            if trade_is_paper != paper:
                continue

        # 1. Status check: trade must be active
        raw_status = str(st.get('status') or '').strip().lower()
        if raw_status in _CLOSED_STATUSES:
            continue
        if raw_status not in _OPEN_STATUSES:
            continue

        # 2. Close metadata check: if close dict or exit finalized, trade is closed
        if st.get('close') is not None:
            continue
        if st.get('settled_at_expiry') is True:
            continue
        if str(st.get('exit_last_step') or '').strip().lower() in ('spread_close_filled', 'finalized_closed'):
            continue

        # 3. Holding quantity check: open trades with 0 holding or 0 filled are not open risk
        if raw_status == 'open':
            holding = st.get('holding_quantity')
            if holding is not None and int(holding or 0) <= 0:
                continue
            filled = st.get('filled_quantity')
            if filled is not None and int(filled or 0) <= 0:
                continue

        # 4. Pending fill check: confirmed cancelled/rejected order with 0 fill is not open risk
        if raw_status == 'pending_fill':
            open_ord = st.get('open_order') or {}
            ord_status = str(open_ord.get('status') or '').lower()
            filled_qty = int(st.get('filled_quantity') or 0)
            if ord_status in ('cancelled', 'canceled', 'rejected') and filled_qty == 0:
                continue

        # 5. Expiry date check: skip trades whose option expiry has already passed
        try:
            from common.session_cleanup import trade_expiry_date, _central_today
            exp_date = trade_expiry_date(st, os.path.basename(path))
            if exp_date and exp_date < _central_today():
                continue
        except Exception:
            pass

        # 6. Side check: puts and calls are independent
        if str(st.get('entry', {}).get('side', '')).upper() != side:
            continue

        short_leg = st.get('short_leg') or {}
        long_leg = st.get('long_leg') or {}
        if not short_leg.get('symbol') or not long_leg.get('symbol'):
            continue

        ex_short = to_tastytrade(short_leg['symbol'])
        ex_long = to_tastytrade(long_leg['symbol'])
        lot = st.get('lot', '?')

        # Identical spread guard: allowed by default unless allow_same_spread=False
        if not allow_same_spread and symbols_equivalent(new_short, ex_short) and symbols_equivalent(new_long, ex_long):
            return (
                f'identical spread {new_short}/{new_long} already open in lot {lot} '
                f'({path})'
            )

        # Leg flip / reversal guards:
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
    allow_same_spread: bool = True,
    max_shifts: int = 30,
    step: int = OVERLAP_SHIFT_STEP,
    direction: str = 'default',
    paper: Optional[bool] = None,
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
        if leg_overlap_conflict(short_sym, long_sym, side, exclude_path=exclude_path, allow_same_spread=allow_same_spread, paper=paper) is None:
            return ss, ls, short_sym, long_sym, shifts

        ss, ls = shift_spread_strikes(side, ss, ls, step, direction=direction)
        shifts += 1
        if ss <= 0 or ls <= 0:
            return None
        if side == 'P' and ls >= ss:
            return None
        if side == 'C' and ls <= ss:
            return None

    return None
