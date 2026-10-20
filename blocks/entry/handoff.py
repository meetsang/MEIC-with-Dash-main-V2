"""Apply session row stop settings after full fill."""
from __future__ import annotations

from typing import Any, Dict

from blocks.session.plan import SessionRow
from blocks.stop.stop_math import apply_two_x_thresholds


def apply_stop_snapshot(state: Dict[str, Any], row: SessionRow) -> None:
    """Set stop multiplier fields from session row before stop monitor handoff."""
    mult = float(row.stop_multiplier or 2)
    state['stop_multiplier'] = mult
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
