"""Fire MEIC entry workers for one tranche lot (tranche-now / app_main)."""
from __future__ import annotations

import logging
import os
from typing import Optional

from blocks.entry.meic_worker import run_meic_entry_row
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.csv_update import apply_entry_result, mark_row_entering
from blocks.session.plan import load_meic_session_today

log = logging.getLogger(__name__)


def fire_meic_lot(root: str, lot: str, *, logger: Optional[logging.Logger] = None) -> bool:
    """
    Run entry workers for all session CSV rows matching ``lot``.

    Returns True when session CSV rows were found and workers were attempted;
    False when no CSV rows exist (caller may fall back to legacy entry).
    """
    lg = logger or log
    bootstrap_meic_session_if_missing(root)
    plan = load_meic_session_today(root)
    if plan is None:
        lg.warning('No MEIC session CSV — cannot fire lot %s', lot)
        return False

    rows = [r for r in plan.rows if r.lot == lot]
    if not rows:
        lg.warning('No session rows for lot %s', lot)
        return False

    for row in rows:
        if row.paused:
            lg.info('Skipping %s — paused in session CSV', row.slot_key)
            continue
        if row.skip:
            lg.info('Skipping %s — skip flag set', row.slot_key)
            continue
        row_log = logging.getLogger(f'entry.{row.slot_key}')
        if row.state == 'pending':
            mark_row_entering(plan.path, row.slot_key, strategy=plan.strategy)
        result = run_meic_entry_row(row, row_log)
        apply_entry_result(plan.path, result, strategy=plan.strategy)
    return True


def project_root_from_caller(start: Optional[str] = None) -> str:
    """Walk up from ``start`` to find repo root (directory containing meic0dte/)."""
    current = os.path.abspath(start or os.path.dirname(__file__))
    while current and current != os.path.dirname(current):
        if os.path.isdir(os.path.join(current, 'meic0dte')):
            return current
        current = os.path.dirname(current)
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
