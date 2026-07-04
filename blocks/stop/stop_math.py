"""Stop multiplier and exchange stop price helpers."""
from __future__ import annotations

from typing import Any, Dict, Optional

import math

import meic0dte.app.config as app_config
from common.option_ticks import round_spx_option_price, spx_option_tick


def stop_multiplier_for_state(
    state: Dict[str, Any],
    *,
    side: Optional[str] = None,
    default: Optional[float] = None,
) -> float:
    """
    Resolve stop multiplier for exchange stop placement.

    Prefers session/plan fields on trade JSON; falls back to legacy STOP_PRCNT_* by side.
    """
    if state.get('stop_multiplier') is not None:
        return float(state['stop_multiplier'])
    plan = state.get('plan') or {}
    if plan.get('stop_multiplier') is not None:
        return float(plan['stop_multiplier'])
    if default is not None:
        return float(default)
    entry = state.get('entry') or {}
    side = side or entry.get('side') or 'P'
    return app_config.STOP_PRCNT_C if side == 'C' else app_config.STOP_PRCNT_P


def exchange_stop_limit_prices(short_fill: float, multiplier: float) -> tuple[float, float]:
    """
    Stop/limit pair for broker STOP_LIMIT orders.

    Tasty rejects mismatched ticks (e.g. stop 2.95 with limit 3.1). When the
    limit lands in the $0.10-tick zone, round the stop trigger to $0.10 too.
    """
    stop = round_spx_option_price((float(short_fill) - 0.10) * float(multiplier))
    limit = round_spx_option_price(stop + app_config.LIMIT_OFFSET)
    if spx_option_tick(limit) >= 0.10:
        stop = round(math.ceil(stop / 0.10) * 0.10, 2)
        limit = round_spx_option_price(stop + app_config.LIMIT_OFFSET)
    return stop, limit


def exchange_stop_price(short_fill: float, multiplier: float) -> float:
    """Initial short-leg STOP_LIMIT trigger: ((short_fill - 0.10) × multiplier), tick rounded."""
    stop, _ = exchange_stop_limit_prices(short_fill, multiplier)
    return stop


def spread_breach_threshold(state: Dict[str, Any]) -> float:
    """
    Spread mid (short − long) that triggers software breach.

    Uses 2× net credit + offset — the spread risk limit — not 2× short leg.
    Exchange stop on the short leg remains a separate broker-side backstop.
    """
    entry = state.get('entry') or {}
    two_x = entry.get('two_x_net_credit')
    if two_x is None or float(two_x) <= 0:
        net = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
        mult = stop_multiplier_for_state(state)
        two_x = round(round(net * mult / 0.05) * 0.05, 2)
    return round(float(two_x) + 0.20, 2)


def theoretical_stop_spread_debit(state: Dict[str, Any]) -> Optional[float]:
    """Operator risk stop: stop× net credit as spread exit debit."""
    entry = state.get('entry') or {}
    two_x = entry.get('two_x_net_credit')
    if two_x is not None and float(two_x) > 0:
        return float(two_x)
    net = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
    if net <= 0:
        return None
    mult = stop_multiplier_for_state(state)
    return round(round(net * mult / 0.05) * 0.05, 2)


def apply_two_x_thresholds(state: Dict[str, Any], multiplier: float) -> None:
    """Update phase-1/2 breach threshold fields from fill prices and multiplier."""
    mult = float(multiplier)
    short_fill = float(state['short_leg'].get('fill_price') or 0)
    net_credit = float(state['entry'].get('net_credit') or 0)
    if short_fill > 0:
        state['short_leg']['two_x_short'] = round(round(short_fill * mult / 0.05) * 0.05, 2)
    if net_credit > 0:
        state['entry']['two_x_net_credit'] = round(round(net_credit * mult / 0.05) * 0.05, 2)
