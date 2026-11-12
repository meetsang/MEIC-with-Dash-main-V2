"""Apply session row stop settings after full fill."""
from __future__ import annotations

from typing import Any, Dict

from blocks.session.plan import SessionRow
from blocks.stop.stop_math import apply_two_x_thresholds, sl_pct_from_stop_multiplier


def apply_stop_snapshot(state: Dict[str, Any], row: SessionRow) -> None:
    """Set stop multiplier fields from session row before stop monitor handoff."""
    is_debit = (row.side or '').upper() in ('PDS', 'CDS') or state.get('spread_type') == 'debit'
    spread_type = 'debit' if is_debit else 'credit'
    default_mult = 0.5 if is_debit else 2.0
    mult = float(row.stop_multiplier if row.stop_multiplier is not None else default_mult)
    state['stop_multiplier'] = mult
    state['stop_loss_pct'] = sl_pct_from_stop_multiplier(mult, spread_type=spread_type)
    if is_debit:
        state['spread_type'] = 'debit'
        state['stop_profile'] = 'manual_debit_spread'
    if 'short_leg' in state:
        apply_two_x_thresholds(state, mult)
    state['status'] = 'open'
    filled = int(state.get('filled_quantity') or 0)
    target = int(state.get('quantity') or getattr(row, 'quantity', 0) or 0)
    if target and filled >= target:
        state.setdefault('open_order', {})['fully_filled'] = True
    pt = getattr(row, 'profit_target_pct', None)
    if pt is not None:
        state['profit_target_pct'] = float(pt)
        if 'exit_config' not in state or not isinstance(state['exit_config'], dict):
            state['exit_config'] = {}
        state['exit_config']['profit_target_pct'] = float(pt)
        state['exit_config']['stop_loss_pct'] = state['stop_loss_pct']
