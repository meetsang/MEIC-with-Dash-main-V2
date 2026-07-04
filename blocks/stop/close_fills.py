"""Close fill slippage — theoretical stop vs brokerage, plus execution efficiency."""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import meic0dte.app.config as app_config
from blocks.stop.stop_math import (
    exchange_stop_price,
    stop_multiplier_for_state,
    theoretical_stop_spread_debit,
)

SOFTWARE_BREACH_MECHANISMS = frozenset({
    'software_breach',
    'spread_stop_breach',
    'breach_limit_reprice',
})

# Close paths that may use stop/breach pipelines (not all qualify for slippage).
STOP_OUT_MECHANISMS = frozenset({
    'software_breach',
    'spread_stop_breach',
    'breach_limit_reprice',
    'exchange_stop',
    'phase2_upgrade',
    'phase3_proximity',
    'breach',
    'stop_filled',
    'manual_close',
    'admin_killswitch',
})

# Operator slippage only applies when the trade exited via stop/breach — not manual kill/close.
OPERATOR_SLIPPAGE_MECHANISMS = frozenset({
    'software_breach',
    'spread_stop_breach',
    'breach_limit_reprice',
    'exchange_stop',
    'phase2_upgrade',
    'phase3_proximity',
    'breach',
    'stop_filled',
})

# Operator kill paths — null long_close_price defaults to 0.0 (V2.9), not open-fill inference.
MANUAL_KILL_MECHANISMS = frozenset({
    'manual_close',
    'admin_killswitch',
})


def _resolved_long_close_price(state: Dict[str, Any]) -> Optional[float]:
    """Long STC fill; manual kill with missing leg uses 0.0 (V2.9), not open long fill."""
    long_close = state.get('long_close_price')
    if long_close is not None:
        return float(long_close)
    if state.get('short_close_price') is None:
        return None
    mechanism = str(state.get('close_mechanism') or '').lower()
    if mechanism in MANUAL_KILL_MECHANISMS:
        return 0.0
    return None


def designated_stop_price(state: Dict[str, Any]) -> Optional[float]:
    """Exchange stop trigger from stop× math — not breach threshold, not broker limit."""
    if state.get('designated_stop_price') is not None:
        return float(state['designated_stop_price'])
    close = state.get('close') or {}
    if close.get('designated_stop_price') is not None:
        return float(close['designated_stop_price'])
    short_fill = float((state.get('short_leg') or {}).get('fill_price') or 0)
    if short_fill <= 0:
        return None
    mult = stop_multiplier_for_state(state)
    return exchange_stop_price(short_fill, mult)


def _is_software_breach_close(state: Dict[str, Any]) -> bool:
    mechanism = str(state.get('close_mechanism') or '').lower()
    if mechanism in SOFTWARE_BREACH_MECHANISMS:
        return True
    return 'breach' in mechanism and mechanism not in ('exchange_stop',)


def _is_stop_out_close(state: Dict[str, Any]) -> bool:
    mechanism = str(state.get('close_mechanism') or '').lower()
    if mechanism in STOP_OUT_MECHANISMS:
        return True
    close = state.get('close') or {}
    reason = str(close.get('reason') or '').lower()
    return reason in STOP_OUT_MECHANISMS


def _qualifies_for_operator_slippage(state: Dict[str, Any]) -> bool:
    """True only for exchange stop / software breach exits — not dashboard kill/close."""
    mechanism = str(state.get('close_mechanism') or '').lower()
    if mechanism in OPERATOR_SLIPPAGE_MECHANISMS:
        return True
    close = state.get('close') or {}
    reason = str(close.get('reason') or '').lower()
    return reason in OPERATOR_SLIPPAGE_MECHANISMS


def brokerage_spread_exit_debit(state: Dict[str, Any]) -> Optional[float]:
    """Net spread exit debit from brokerage leg fills (short BTC − long STC)."""
    short_fill = state.get('short_close_price')
    long_fill = _resolved_long_close_price(state)
    if short_fill is None or long_fill is None:
        return None
    return round(float(short_fill) - float(long_fill), 2)


def stop_out_slippage_per_spread(state: Dict[str, Any]) -> Optional[float]:
    """
    Operator slippage: theoretical set stop (stop× net credit) vs brokerage exit.

    Positive = exited at or better than theoretical stop (lower debit = good for PnL).
    Only computed for stop-out closes with both leg fills.
    """
    if not _qualifies_for_operator_slippage(state):
        return None
    theoretical = theoretical_stop_spread_debit(state)
    actual = brokerage_spread_exit_debit(state)
    if theoretical is None or actual is None:
        return None
    return round(float(theoretical) - float(actual), 2)


def stop_slippage_short(state: Dict[str, Any]) -> Optional[float]:
    """
    Short-leg execution efficiency vs exchange stop trigger (positive = helped).

    Software breach: fixed policy uplift vs designated (not fill vs broker limit).
    """
    designated = designated_stop_price(state)
    if designated is None:
        return None
    if _is_software_breach_close(state):
        uplift = float(
            state.get('software_breach_uplift') or app_config.SOFTWARE_BREACH_SLIPPAGE_UPLIFT,
        )
        return round(-uplift, 2)
    fill = state.get('short_close_price')
    if fill is None:
        return None
    return round(float(designated) - float(fill), 2)


def short_close_order_price(state: Dict[str, Any]) -> Optional[float]:
    """Reference price for short-leg BTC (stop/limit we sent)."""
    if state.get('short_close_limit_price') is not None:
        return float(state['short_close_limit_price'])
    active = state.get('active_stop') or {}
    if active.get('limit_price') is not None:
        return float(active['limit_price'])
    if active.get('stop_price') is not None:
        return float(active['stop_price'])
    return None


def long_close_order_price(state: Dict[str, Any]) -> Optional[float]:
    """Reference limit for long-leg STC."""
    if state.get('long_close_limit_price') is not None:
        return float(state['long_close_limit_price'])
    return None


def exit_slippage_per_spread(state: Dict[str, Any]) -> Optional[float]:
    """
    Execution efficiency: net exit debit at order prices vs brokerage fills.

    Positive = filled better than working limits (not the operator stop-out metric).
    """
    short_order = short_close_order_price(state)
    long_order = long_close_order_price(state)
    short_fill = state.get('short_close_price')
    long_fill = state.get('long_close_price')
    if None in (short_order, long_order, short_fill, long_fill):
        return None
    order_exit_debit = float(short_order) - float(long_order)
    fill_exit_debit = float(short_fill) - float(long_fill)
    return round(order_exit_debit - fill_exit_debit, 2)


def leg_slippage(state: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Per-leg execution efficiency (positive = favorable). Short BTC, long STC."""
    short_order = short_close_order_price(state)
    long_order = long_close_order_price(state)
    short_fill = state.get('short_close_price')
    long_fill = state.get('long_close_price')
    short_slip = None
    long_slip = None
    if short_order is not None and short_fill is not None:
        short_slip = round(float(short_order) - float(short_fill), 2)
    if long_order is not None and long_fill is not None:
        long_slip = round(float(long_fill) - float(long_order), 2)
    return short_slip, long_slip


def slippage_per_spread(state: Dict[str, Any]) -> Optional[float]:
    """Theoretical set stop vs brokerage spread exit (operator slippage)."""
    return stop_out_slippage_per_spread(state)


def slippage_dollars(state: Dict[str, Any]) -> Optional[float]:
    """Operator slippage in dollars — stop/breach exits only; else None when closed."""
    if not _qualifies_for_operator_slippage(state):
        return None
    slip = slippage_per_spread(state)
    if slip is None:
        return None
    qty = int(state.get('filled_quantity') or state.get('quantity') or 1)
    return round(float(slip) * qty * 100, 2)


def slippage_label(dollars: Optional[float]) -> str:
    """Format total slippage in dollars (premium × qty × 100)."""
    if dollars is None:
        return ''
    if float(dollars) == 0:
        return '$0.00'
    v = float(dollars)
    if v >= 0:
        return f'+${v:.2f}'
    return f'-${abs(v):.2f}'


def apply_close_slippage_fields(state: Dict[str, Any]) -> None:
    """Persist slippage on state and nested close snapshot."""
    short_slip, long_slip = leg_slippage(state)
    exec_slip = exit_slippage_per_spread(state)
    stop_slip = stop_slippage_short(state)
    operator_slip = stop_out_slippage_per_spread(state)
    theoretical = theoretical_stop_spread_debit(state)
    brokerage_exit = brokerage_spread_exit_debit(state)
    designated = designated_stop_price(state)
    if designated is not None:
        state['designated_stop_price'] = designated
    if theoretical is not None:
        state['theoretical_stop_spread_debit'] = theoretical
    if brokerage_exit is not None:
        state['brokerage_spread_exit_debit'] = brokerage_exit
    if short_slip is not None:
        state['short_close_slippage'] = short_slip
    if long_slip is not None:
        state['long_close_slippage'] = long_slip
    if exec_slip is not None:
        state['exit_slippage'] = exec_slip
    if stop_slip is not None:
        state['stop_slippage'] = stop_slip
    if operator_slip is not None:
        state['slippage'] = operator_slip
    else:
        state.pop('slippage', None)
    close = state.get('close')
    if isinstance(close, dict):
        if designated is not None:
            close['designated_stop_price'] = designated
        if theoretical is not None:
            close['theoretical_stop_spread_debit'] = theoretical
        if brokerage_exit is not None:
            close['brokerage_spread_exit_debit'] = brokerage_exit
        if short_slip is not None:
            close['short_close_slippage'] = short_slip
        if long_slip is not None:
            close['long_close_slippage'] = long_slip
        if exec_slip is not None:
            close['exit_slippage'] = exec_slip
        if stop_slip is not None:
            close['stop_slippage'] = stop_slip
        if operator_slip is not None:
            close['slippage'] = operator_slip
        else:
            close.pop('slippage', None)
        if state.get('short_close_limit_price') is not None:
            close['short_close_limit_price'] = state['short_close_limit_price']
        if state.get('long_close_limit_price') is not None:
            close['long_close_limit_price'] = state['long_close_limit_price']
