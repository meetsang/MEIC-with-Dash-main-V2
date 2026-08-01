"""Spread stop-breach math — strategy-agnostic credit/debit spread defaults."""
from __future__ import annotations


def spread_mark_price(
    short_leg_mid: float,
    long_leg_mid: float,
    *,
    spread_type: str = 'credit',
) -> float:
    """Mark price for a vertical spread.

    credit: cost to buy back (short mid minus long mid)
    debit:  position value (long mid minus short mid)
    """
    if spread_type == 'debit':
        return round(long_leg_mid - short_leg_mid, 2)
    return round(short_leg_mid - long_leg_mid, 2)


def spread_breach_triggered(
    spread_price: float,
    stop_threshold: float,
    *,
    spread_type: str = 'credit',
) -> bool:
    """True when the spread has breached its stop threshold."""
    if spread_type == 'debit':
        # Debit: loss when value falls below threshold
        return spread_price <= stop_threshold
    return spread_price >= stop_threshold


def profit_target_hit(
    current_mark: float,
    entry_price: float,
    target_pct: float,
    *,
    spread_type: str = 'credit',
) -> bool:
    """True when profit target is reached."""
    if entry_price <= 0 or target_pct is None:
        return False
    if spread_type == 'debit':
        target = entry_price * (1 + float(target_pct))
        return current_mark >= target
    target = entry_price * (1 - float(target_pct))
    return current_mark <= target
