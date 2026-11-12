"""Stop multiplier and exchange stop price helpers."""
from __future__ import annotations

from typing import Any, Dict, Optional

import math

import meic0dte.app.config as app_config
from common.option_ticks import round_spx_option_price, spx_option_tick


def stop_multiplier_from_sl_pct(sl_pct: float, *, spread_type: str = 'credit') -> float:
    """Convert DRMA-style net stop-loss ratio (e.g. 1.0 = 100% SL) to stop_multiplier.

    Credit spread: mult = 1.0 + sl_pct  (100% SL -> 2.0x credit, 200% SL -> 3.0x credit)
    Debit spread:  mult = 1.0 - sl_pct  (50% SL  -> 0.5x debit,  33.5% SL -> 0.665x debit)
    """
    pct = max(0.0, float(sl_pct))
    if spread_type == 'debit':
        return round(max(0.0, 1.0 - pct), 6)
    return round(max(0.05, 1.0 + pct), 6)


def sl_pct_from_stop_multiplier(multiplier: float, *, spread_type: str = 'credit') -> float:
    """Convert stop_multiplier back to DRMA-style net stop-loss ratio."""
    mult = float(multiplier)
    if spread_type == 'debit':
        return round(max(0.0, 1.0 - mult), 6)
    return round(max(0.0, mult - 1.0), 6)


def stop_loss_pct_for_state(state: Dict[str, Any]) -> float:
    """Resolve stop_loss_pct ratio (e.g. 1.0 = 100%, 2.0 = 200%, 0.5 = 50%) from trade state."""
    spread_type = str(state.get('spread_type') or 'credit')
    if state.get('stop_loss_pct') is not None:
        return float(state['stop_loss_pct'])
    plan = state.get('plan') or {}
    if plan.get('stop_loss_pct') is not None:
        return float(plan['stop_loss_pct'])
    mult = stop_multiplier_for_state(state)
    return sl_pct_from_stop_multiplier(mult, spread_type=spread_type)


def stop_multiplier_for_state(
    state: Dict[str, Any],
    *,
    side: Optional[str] = None,
    default: Optional[float] = None,
) -> float:
    """
    Resolve stop multiplier for exchange stop placement and spread breach math.

    Prefers session/plan fields on trade JSON; falls back to legacy STOP_PRCNT_* by side.
    """
    spread_type = str(state.get('spread_type') or 'credit')
    if state.get('stop_multiplier') is not None:
        return float(state['stop_multiplier'])
    if state.get('stop_loss_pct') is not None:
        return stop_multiplier_from_sl_pct(float(state['stop_loss_pct']), spread_type=spread_type)
    plan = state.get('plan') or {}
    if plan.get('stop_multiplier') is not None:
        return float(plan['stop_multiplier'])
    if plan.get('stop_loss_pct') is not None:
        return stop_multiplier_from_sl_pct(float(plan['stop_loss_pct']), spread_type=spread_type)
    if default is not None:
        return float(default)
    if spread_type == 'debit':
        return 0.5
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
    Spread mid that triggers software breach.

    Credit spread: 2× net credit + offset (short − long >= threshold).
    Debit spread:  net_debit × (1 − sl_pct) without +0.20 offset (long − short <= threshold).
    """
    entry = state.get('entry') or {}
    spread_type = str(state.get('spread_type') or 'credit')
    two_x = entry.get('two_x_net_credit')
    if spread_type == 'debit':
        if two_x is None or float(two_x) <= 0:
            net = float(
                entry.get('net_debit')
                or entry.get('limit_debit')
                or entry.get('net_credit')
                or entry.get('limit_credit')
                or 0
            )
            mult = stop_multiplier_for_state(state, default=0.5)
            two_x = round_spx_option_price(max(0.0, net * mult))
        return round(float(two_x), 2)
    if two_x is None or float(two_x) <= 0:
        net = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
        mult = stop_multiplier_for_state(state)
        two_x = round(round(net * mult / 0.05) * 0.05, 2)
    return round(float(two_x) + 0.20, 2)


def theoretical_stop_spread_debit(state: Dict[str, Any]) -> Optional[float]:
    """Operator risk stop: stop× net credit (or debit stop mark) as spread exit level."""
    entry = state.get('entry') or {}
    two_x = entry.get('two_x_net_credit')
    if two_x is not None and float(two_x) > 0:
        return float(two_x)
    spread_type = str(state.get('spread_type') or 'credit')
    if spread_type == 'debit':
        net = float(
            entry.get('net_debit')
            or entry.get('limit_debit')
            or entry.get('net_credit')
            or entry.get('limit_credit')
            or 0
        )
        if net <= 0:
            return None
        mult = stop_multiplier_for_state(state, default=0.5)
        return round_spx_option_price(max(0.0, net * mult))
    net = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
    if net <= 0:
        return None
    mult = stop_multiplier_for_state(state)
    return round(round(net * mult / 0.05) * 0.05, 2)


def apply_two_x_thresholds(state: Dict[str, Any], multiplier: Optional[float] = None) -> None:
    """Update phase-1/2 breach threshold fields from fill prices and multiplier."""
    mult = float(multiplier) if multiplier is not None else stop_multiplier_for_state(state)
    spread_type = str(state.get('spread_type') or 'credit')
    if spread_type == 'debit':
        entry = state.setdefault('entry', {})
        net_debit = float(
            entry.get('net_debit')
            or entry.get('limit_debit')
            or entry.get('net_credit')
            or entry.get('limit_credit')
            or 0
        )
        if net_debit > 0:
            entry['two_x_net_credit'] = round_spx_option_price(max(0.0, net_debit * mult))
        return
    short_fill = float(state['short_leg'].get('fill_price') or 0)
    net_credit = float(state['entry'].get('net_credit') or 0)
    if short_fill > 0:
        state['short_leg']['two_x_short'] = round(round(short_fill * mult / 0.05) * 0.05, 2)
    if net_credit > 0:
        state['entry']['two_x_net_credit'] = round(round(net_credit * mult / 0.05) * 0.05, 2)
