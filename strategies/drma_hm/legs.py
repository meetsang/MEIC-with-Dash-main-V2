"""Option strike selection, delta modeling, and ComboLeg builder for DRMA_HM."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from brokers.base import ComboLeg
from common.symbols import build_tastytrade_symbol


def round_atm(spot: float, rounding: int = 5) -> int:
    """Round spot to nearest strike interval."""
    return int(round(float(spot) / rounding) * rounding)


def estimate_0dte_delta(
    spot: float,
    strike: float,
    opt_type: str,
    iv: float = 0.20,
    dte_hours: float = 5.0,
) -> float:
    """Black-Scholes analytical delta estimate for 0DTE options."""
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.50 if opt_type.upper() == 'C' else -0.50

    t = max(0.1, float(dte_hours)) / (365.0 * 24.0)
    denom = iv * math.sqrt(t)
    if denom <= 0:
        return 1.0 if (opt_type.upper() == 'C' and spot > strike) else 0.0

    d1 = (math.log(spot / strike) + 0.5 * (iv ** 2) * t) / denom
    norm_cdf = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))

    if opt_type.upper() == 'C':
        return round(max(0.001, min(0.999, norm_cdf)), 4)
    else:
        return round(max(-0.999, min(-0.001, norm_cdf - 1.0)), 4)


def estimate_option_price(
    spot: float,
    strike: float,
    opt_type: str,
    iv: float = 0.20,
    dte_hours: float = 5.0,
) -> float:
    """Black-Scholes analytical price estimate for 0DTE options."""
    if spot <= 0 or strike <= 0 or iv <= 0:
        return 0.0
    t = max(0.1, float(dte_hours)) / (365.0 * 24.0)
    denom = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * (iv ** 2) * t) / denom
    d2 = d1 - denom
    norm_d1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    norm_d2 = 0.5 * (1.0 + math.erf(d2 / math.sqrt(2.0)))

    if opt_type.upper() == 'C':
        price = spot * norm_d1 - strike * norm_d2
    else:
        price = strike * (1.0 - norm_d2) - spot * (1.0 - norm_d1)
    return round(max(0.05, price), 2)


def select_non_directional_strikes(
    side: str,                  # 'P' for Bull Put Spread, 'C' for Bear Call Spread
    spot: float,
    spread_width: int = 10,
    *,
    selection_mode: str = 'credit', # 'credit' or 'delta'
    target_delta: float = 0.15,
    min_delta: float = 0.08,
    max_delta: float = 0.25,
    target_credit: float = 1.00,
    min_credit: float = 0.40,
    expiry_yymmdd: str = '',
    live_mids: Optional[Dict[str, float]] = None,
    live_deltas: Optional[Dict[str, float]] = None,
    estimated_iv: float = 0.20,
    dte_hours: float = 5.0,
) -> Tuple[int, int, float, float]:
    """Select (short_strike, long_strike, selected_delta, estimated_credit) for credit spread.

    For Put Credit Spread (side='P'):
        - Short strike is below spot (OTM Put)
        - Long strike = short_strike - spread_width
    For Call Credit Spread (side='C'):
        - Short strike is above spot (OTM Call)
        - Long strike = short_strike + spread_width
    """
    side = side.upper()
    atm = round_atm(spot, 5)
    candidates = []

    if side == 'C':
        # Search call strikes above spot (OTM Calls)
        for short_k in range(atm + 5, atm + 125, 5):
            long_k = short_k + spread_width
            short_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', short_k) if expiry_yymmdd else ''
            long_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', long_k) if expiry_yymmdd else ''

            delta = live_deltas.get(short_sym) if (live_deltas and short_sym in live_deltas) else None
            if delta is None:
                delta = estimate_0dte_delta(spot, short_k, 'C', iv=estimated_iv, dte_hours=dte_hours)

            short_mid = live_mids.get(short_sym) if (live_mids and short_sym in live_mids) else None
            long_mid = live_mids.get(long_sym) if (live_mids and long_sym in live_mids) else None

            if short_mid is not None and long_mid is not None:
                credit = round(max(0.0, short_mid - long_mid), 2)
            else:
                p_short = estimate_option_price(spot, short_k, 'C', iv=estimated_iv, dte_hours=dte_hours)
                p_long = estimate_option_price(spot, long_k, 'C', iv=estimated_iv, dte_hours=dte_hours)
                credit = round(max(0.05, p_short - p_long), 2)

            candidates.append((short_k, long_k, float(delta), float(credit)))

    else:
        # Search put strikes below spot (OTM Puts)
        for short_k in range(atm - 5, atm - 125, -5):
            long_k = short_k - spread_width
            short_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', short_k) if expiry_yymmdd else ''
            long_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', long_k) if expiry_yymmdd else ''

            delta = live_deltas.get(short_sym) if (live_deltas and short_sym in live_deltas) else None
            if delta is None:
                delta = estimate_0dte_delta(spot, short_k, 'P', iv=estimated_iv, dte_hours=dte_hours)

            short_mid = live_mids.get(short_sym) if (live_mids and short_sym in live_mids) else None
            long_mid = live_mids.get(long_sym) if (live_mids and long_sym in live_mids) else None

            if short_mid is not None and long_mid is not None:
                credit = round(max(0.0, short_mid - long_mid), 2)
            else:
                p_short = estimate_option_price(spot, short_k, 'P', iv=estimated_iv, dte_hours=dte_hours)
                p_long = estimate_option_price(spot, long_k, 'P', iv=estimated_iv, dte_hours=dte_hours)
                credit = round(max(0.05, p_short - p_long), 2)

            candidates.append((short_k, long_k, float(delta), float(credit)))

    if not candidates:
        fallback_short = atm + 15 if side == 'C' else atm - 15
        fallback_long = fallback_short + spread_width if side == 'C' else fallback_short - spread_width
        return fallback_short, fallback_long, 0.15, 1.00

    if selection_mode == 'credit':
        # Select spread closest to target_credit with at least min_credit
        valid = [c for c in candidates if c[3] >= min_credit]
        pool = valid if valid else candidates
        best = min(pool, key=lambda c: abs(c[3] - target_credit))
        return best[0], best[1], round(best[2], 4), round(best[3], 2)
    else:
        # Select spread by delta target within min_delta..max_delta
        target_abs = abs(target_delta)
        in_band = [c for c in candidates if min_delta <= abs(c[2]) <= max_delta]
        pool = in_band if in_band else candidates
        best = min(pool, key=lambda c: abs(abs(c[2]) - target_abs))
        return best[0], best[1], round(best[2], 4), round(best[3], 2)


def select_directional_strikes(
    side: str,                  # 'C' for Bull Call Debit Spread, 'P' for Bear Put Debit Spread
    spot: float,
    spread_width: int = 10,
    *,
    selection_mode: str = 'debit', # 'debit' or 'delta'
    target_delta: float = 0.40,
    min_delta: float = 0.25,
    max_delta: float = 0.55,
    target_debit: float = 2.50,
    max_debit: float = 4.00,
    expiry_yymmdd: str = '',
    live_mids: Optional[Dict[str, float]] = None,
    live_deltas: Optional[Dict[str, float]] = None,
    estimated_iv: float = 0.20,
    dte_hours: float = 5.0,
) -> Tuple[int, int, float, float]:
    """Select (long_strike, short_strike, selected_delta, estimated_debit) for debit spread.

    For Bull Call Debit Spread (side='C'):
        - Long Call is near ATM / slightly OTM (e.g. ~40 delta)
        - Short Call = long_strike + spread_width
    For Bear Put Debit Spread (side='P'):
        - Long Put is near ATM / slightly OTM (e.g. ~40 delta)
        - Short Put = long_strike - spread_width
    """
    side = side.upper()
    atm = round_atm(spot, 5)
    candidates = []

    if side == 'C':
        # Calls around ATM and slightly OTM
        for long_k in range(atm - 15, atm + 40, 5):
            short_k = long_k + spread_width
            long_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', long_k) if expiry_yymmdd else ''
            short_sym = build_tastytrade_symbol(expiry_yymmdd, 'C', short_k) if expiry_yymmdd else ''

            delta = live_deltas.get(long_sym) if (live_deltas and long_sym in live_deltas) else None
            if delta is None:
                delta = estimate_0dte_delta(spot, long_k, 'C', iv=estimated_iv, dte_hours=dte_hours)

            long_mid = live_mids.get(long_sym) if (live_mids and long_sym in live_mids) else None
            short_mid = live_mids.get(short_sym) if (live_mids and short_sym in live_mids) else None

            if long_mid is not None and short_mid is not None:
                debit = round(max(0.10, long_mid - short_mid), 2)
            else:
                p_long = estimate_option_price(spot, long_k, 'C', iv=estimated_iv, dte_hours=dte_hours)
                p_short = estimate_option_price(spot, short_k, 'C', iv=estimated_iv, dte_hours=dte_hours)
                debit = round(max(0.20, p_long - p_short), 2)

            candidates.append((long_k, short_k, float(delta), float(debit)))

    else:
        # Puts around ATM and slightly OTM
        for long_k in range(atm + 15, atm - 40, -5):
            short_k = long_k - spread_width
            long_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', long_k) if expiry_yymmdd else ''
            short_sym = build_tastytrade_symbol(expiry_yymmdd, 'P', short_k) if expiry_yymmdd else ''

            delta = live_deltas.get(long_sym) if (live_deltas and long_sym in live_deltas) else None
            if delta is None:
                delta = estimate_0dte_delta(spot, long_k, 'P', iv=estimated_iv, dte_hours=dte_hours)

            long_mid = live_mids.get(long_sym) if (live_mids and long_sym in live_mids) else None
            short_mid = live_mids.get(short_sym) if (live_mids and short_sym in live_mids) else None

            if long_mid is not None and short_mid is not None:
                debit = round(max(0.10, long_mid - short_mid), 2)
            else:
                p_long = estimate_option_price(spot, long_k, 'P', iv=estimated_iv, dte_hours=dte_hours)
                p_short = estimate_option_price(spot, short_k, 'P', iv=estimated_iv, dte_hours=dte_hours)
                debit = round(max(0.20, p_long - p_short), 2)

            candidates.append((long_k, short_k, float(delta), float(debit)))

    if not candidates:
        fallback_long = atm
        fallback_short = atm + spread_width if side == 'C' else atm - spread_width
        return fallback_long, fallback_short, 0.40, 2.50

    if selection_mode == 'debit':
        # Select spread closest to target_debit not exceeding max_debit
        valid = [c for c in candidates if c[3] <= max_debit]
        pool = valid if valid else candidates
        best = min(pool, key=lambda c: abs(c[3] - target_debit))
        return best[0], best[1], round(best[2], 4), round(best[3], 2)
    else:
        # Select by delta target within min_delta..max_delta
        target_abs = abs(target_delta)
        in_band = [c for c in candidates if min_delta <= abs(c[2]) <= max_delta]
        pool = in_band if in_band else candidates
        best = min(pool, key=lambda c: abs(abs(c[2]) - target_abs))
        return best[0], best[1], round(best[2], 4), round(best[3], 2)


def build_credit_spread_legs(
    short_strike: int,
    long_strike: int,
    opt_type: str,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """Construct ComboLeg objects for an opening credit spread."""
    short_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type.upper(), short_strike)
    long_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type.upper(), long_strike)
    return [
        ComboLeg(symbol=short_sym, action='SELL_TO_OPEN', quantity=quantity),
        ComboLeg(symbol=long_sym, action='BUY_TO_OPEN', quantity=quantity),
    ]


def build_debit_spread_legs(
    long_strike: int,
    short_strike: int,
    opt_type: str,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """Construct ComboLeg objects for an opening debit spread."""
    long_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type.upper(), long_strike)
    short_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type.upper(), short_strike)
    return [
        ComboLeg(symbol=long_sym, action='BUY_TO_OPEN', quantity=quantity),
        ComboLeg(symbol=short_sym, action='SELL_TO_OPEN', quantity=quantity),
    ]


def build_closing_legs(open_legs: List[ComboLeg], quantity: int = 1) -> List[ComboLeg]:
    """Construct closing ComboLegs by inverting actions."""
    closing = []
    for leg in open_legs:
        act = leg.action
        if 'BUY' in act:
            close_act = 'SELL_TO_CLOSE'
        else:
            close_act = 'BUY_TO_CLOSE'
        closing.append(ComboLeg(symbol=leg.symbol, action=close_act, quantity=quantity))
    return closing
