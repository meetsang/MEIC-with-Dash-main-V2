"""Spread stop-breach math — strategy-agnostic credit spread defaults."""
from __future__ import annotations


def spread_mark_price(short_leg_mid: float, long_leg_mid: float) -> float:
    """Cost to buy back the spread (short mid minus long mid)."""
    return round(short_leg_mid - long_leg_mid, 2)


def spread_breach_triggered(spread_price: float, stop_threshold: float) -> bool:
    """True when spread cost to close >= stop threshold."""
    return spread_price >= stop_threshold
