"""MEIC entry chase sequence helpers (testable without broker)."""
from __future__ import annotations

import meic0dte.app.config as meic_config
from blocks.session.plan import SessionRow


def chase_kind(attempt: int, row: SessionRow) -> str:
    """Return chase phase for 1-based attempt number."""
    if attempt == 1:
        return 'initial_scan'
    if attempt <= 1 + int(row.chase1_max):
        return row.chase1_mode or 'chase_same_trade'
    if attempt <= 1 + int(row.chase1_max) + int(row.chase2_max):
        return row.chase2_mode or 'build_new_strikes'
    return 'exhausted'


def max_entry_attempts(row: SessionRow) -> int:
    """Cap attempts by row max and chase phase totals."""
    cap = 1 + int(row.chase1_max) + int(row.chase2_max)
    return min(int(row.max_attempts), cap)


def chase_credit_step(credit: float, step: float | None = None) -> float:
    """Lower spread credit limit by one chase step ($0.05 default)."""
    delta = step if step is not None else meic_config.OPEN_PRICE_ADJ
    return round(credit - delta, 2)


def should_chase_on_unfilled(filled_quantity: int, on_unfilled: str) -> bool:
    """Chase only when zero fill and mode is not none."""
    if filled_quantity > 0:
        return False
    return (on_unfilled or '').lower() != 'none'
