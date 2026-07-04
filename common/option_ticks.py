"""CBOE SPX single-leg option minimum price increments."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

# Premium below $3.00 → $0.05; at $3.00 and above → $0.10 (Cboe SPXW).
SPX_OPTION_TICK_THRESHOLD = 3.0
SPX_TICK_BELOW_THRESHOLD = 0.05
SPX_TICK_AT_OR_ABOVE_THRESHOLD = 0.10


def spx_option_tick(price: float) -> float:
    """Return the minimum tick for a single-leg premium."""
    return (
        SPX_TICK_BELOW_THRESHOLD
        if abs(float(price)) < SPX_OPTION_TICK_THRESHOLD
        else SPX_TICK_AT_OR_ABOVE_THRESHOLD
    )


def round_spx_option_price(price: float, *, minimum: float = 0.05) -> float:
    """Round a debit/credit magnitude to a valid SPX single-leg tick."""
    magnitude = abs(float(price))
    tick = spx_option_tick(magnitude)
    units = (Decimal(str(magnitude)) / Decimal(str(tick))).quantize(
        Decimal('1'), rounding=ROUND_HALF_UP
    )
    rounded = float(units * Decimal(str(tick)))
    return max(minimum, round(rounded, 2))


def step_down_spx_option_price(price: float, *, minimum: float = 0.05) -> float:
    """One SPX tick lower — used to make SELL_TO_CLOSE limits more aggressive."""
    magnitude = abs(float(price))
    tick = spx_option_tick(magnitude)
    stepped = round(magnitude - tick, 2)
    if stepped < minimum:
        return minimum
    return round_spx_option_price(stepped, minimum=minimum)
