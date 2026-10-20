"""Mean reversion leg builder, delta estimations, and expected move math."""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

from brokers.base import ComboLeg
from common.symbols import build_tastytrade_symbol


def round_atm(spot: float, rounding: int = 5) -> int:
    """Round SPX price to the nearest strike interval (typically 5 points)."""
    return int(round(float(spot) / rounding) * rounding)


def compute_expected_move(straddle_price: float, multiplier: float = 1.0) -> float:
    """Calculate expected move from the 0DTE ATM straddle price."""
    return round(float(straddle_price) * float(multiplier), 2)


def compute_expected_move_boundaries(open_spx: float, expected_move: float) -> Tuple[float, float]:
    """Calculate Upper and Lower Expected Move price levels.

    Returns (upper_em, lower_em).
    """
    upper = round(float(open_spx) + float(expected_move), 2)
    lower = round(float(open_spx) - float(expected_move), 2)
    return upper, lower


def estimate_0dte_delta(
    spot: float,
    strike: float,
    opt_type: str,
    iv: float = 0.20,
    dte_hours: float = 6.0,
) -> float:
    """Analytical Black-Scholes delta estimate for 0DTE SPX options.

    Allows reliable strike selection even when live streamer Greeks are unavailable.
    """
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


def select_credit_spread_strikes(
    side: str,
    spot: float,
    spread_width: int = 10,
    *,
    target_delta: float = 0.18,
    min_delta: float = 0.10,
    max_delta: float = 0.25,
    expiry_yymmdd: str = '',
    live_deltas: Optional[Dict[str, float]] = None,
    estimated_iv: float = 0.20,
    dte_hours: float = 6.0,
) -> Tuple[int, int, float]:
    """Select short and long strikes for 0DTE mean reversion credit spread.

    For side='C' (Bear Call Spread on upper EM breach):
        - Short strike is OTM (above spot), delta in [min_delta, max_delta] (default ~10-25 delta).
        - Long strike = short_strike + spread_width.
    For side='P' (Bull Put Spread on lower EM breach):
        - Short strike is OTM (below spot), abs(delta) in [min_delta, max_delta].
        - Long strike = short_strike - spread_width.

    Returns:
        (short_strike, long_strike, selected_delta)
    """
    side = side.upper()
    atm = round_atm(spot, 5)
    candidates = []

    if side == 'C':
        # Search call strikes above spot (OTM)
        for strike in range(atm + 5, atm + 125, 5):
            sym = build_tastytrade_symbol(expiry_yymmdd, 'C', strike) if expiry_yymmdd else ''
            delta = None
            if live_deltas and sym in live_deltas:
                delta = live_deltas[sym]
            if delta is None:
                delta = estimate_0dte_delta(spot, strike, 'C', iv=estimated_iv, dte_hours=dte_hours)
            candidates.append((strike, float(delta)))

        # Filter within delta band
        in_band = [c for c in candidates if min_delta <= c[1] <= max_delta]
        if in_band:
            best = min(in_band, key=lambda c: abs(c[1] - target_delta))
        else:
            # Fallback: pick strike with delta <= max_delta closest to target
            otm_eligible = [c for c in candidates if c[1] <= max_delta]
            best = min(otm_eligible, key=lambda c: abs(c[1] - target_delta)) if otm_eligible else candidates[0]

        short_k = best[0]
        long_k = short_k + spread_width
        return short_k, long_k, round(best[1], 4)

    elif side == 'P':
        # Search put strikes below spot (OTM)
        for strike in range(atm - 5, atm - 125, -5):
            sym = build_tastytrade_symbol(expiry_yymmdd, 'P', strike) if expiry_yymmdd else ''
            delta = None
            if live_deltas and sym in live_deltas:
                delta = live_deltas[sym]
            if delta is None:
                delta = estimate_0dte_delta(spot, strike, 'P', iv=estimated_iv, dte_hours=dte_hours)
            candidates.append((strike, float(delta)))

        # Filter within abs delta band [min_delta, max_delta]
        in_band = [c for c in candidates if min_delta <= abs(c[1]) <= max_delta]
        if in_band:
            best = min(in_band, key=lambda c: abs(abs(c[1]) - target_delta))
        else:
            # Fallback: pick strike with abs(delta) <= max_delta closest to target
            otm_eligible = [c for c in candidates if abs(c[1]) <= max_delta]
            best = min(otm_eligible, key=lambda c: abs(abs(c[1]) - target_delta)) if otm_eligible else candidates[0]

        short_k = best[0]
        long_k = short_k - spread_width
        return short_k, long_k, round(best[1], 4)

    else:
        raise ValueError(f"Unknown side: {side}. Must be 'C' or 'P'")


def build_spread_legs(
    short_strike: int,
    long_strike: int,
    opt_type: str,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """Build open order legs for the vertical credit spread."""
    opt_type = opt_type.upper()
    short_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, short_strike)
    long_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, long_strike)

    return [
        ComboLeg(symbol=short_sym, action='SELL_TO_OPEN', quantity=quantity),
        ComboLeg(symbol=long_sym, action='BUY_TO_OPEN', quantity=quantity),
    ]


def build_closing_legs(
    short_strike: int,
    long_strike: int,
    opt_type: str,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """Build closing order legs for the vertical credit spread."""
    opt_type = opt_type.upper()
    short_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, short_strike)
    long_sym = build_tastytrade_symbol(expiry_yymmdd, opt_type, long_strike)

    return [
        ComboLeg(symbol=short_sym, action='BUY_TO_CLOSE', quantity=quantity),
        ComboLeg(symbol=long_sym, action='SELL_TO_CLOSE', quantity=quantity),
    ]
