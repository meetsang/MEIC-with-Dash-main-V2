"""Serialized session CSV updates — Entry Monitor is the sole writer after workers finish."""
from __future__ import annotations

import logging
import threading
from typing import Any

from blocks.entry.result import EntryWorkerResult
from blocks.session.plan import SessionPlan

log = logging.getLogger(__name__)

_csv_lock = threading.Lock()


def apply_entry_result(plan_path: str, result: EntryWorkerResult, *, strategy: str) -> None:
    """Reload session CSV from disk, patch one row, save (thread-safe)."""
    if not result or not result.slot_key:
        return
    fields: dict[str, Any] = {'state': result.state}
    if result.trade_path:
        fields['trade_path'] = result.trade_path
    with _csv_lock:
        plan = SessionPlan.load(plan_path, strategy=strategy)
        plan.update_row(result.slot_key, **fields)
        plan.save()
    log.info(
        'Session CSV %s → %s state=%s path=%s',
        result.slot_key,
        plan_path,
        result.state,
        result.trade_path or '(none)',
    )


def mark_row_entering(plan_path: str, slot_key: str, *, strategy: str) -> None:
    with _csv_lock:
        plan = SessionPlan.load(plan_path, strategy=strategy)
        plan.update_row(slot_key, state='entering')
        plan.save()


def try_claim_manual_row(plan_path: str, slot_key: str, *, strategy: str) -> bool:
    """Atomically claim a manual row for entry (entering → placing). Returns False if taken."""
    with _csv_lock:
        plan = SessionPlan.load(plan_path, strategy=strategy)
        row = plan.row_by_slot_key(slot_key)
        if row is None or row.trade_path:
            return False
        if row.state != 'entering':
            return False
        plan.update_row(slot_key, state='placing')
        plan.save()
    return True
