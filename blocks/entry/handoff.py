"""Apply session row stop settings after full fill."""
from __future__ import annotations

from typing import Any, Dict

from blocks.session.plan import SessionRow
from blocks.stop.stop_math import apply_two_x_thresholds


def apply_stop_snapshot(state: Dict[str, Any], row: SessionRow) -> None:
    """Set stop multiplier fields from session row before stop monitor handoff."""
    mult = float(row.stop_multiplier or 2)
    state['stop_multiplier'] = mult
    apply_two_x_thresholds(state, mult)
    state['status'] = 'open'
    state.setdefault('open_order', {})['fully_filled'] = True
