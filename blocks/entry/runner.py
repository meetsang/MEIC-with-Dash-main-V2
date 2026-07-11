"""Supervisor for MEIC and manual session entry workers."""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Set

from blocks.entry.manual_worker import run_manual_entry_row
from blocks.entry.meic_worker import run_meic_entry_row
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.csv_update import apply_entry_result, mark_row_entering, try_claim_manual_row
from blocks.session.plan import load_manual_session_today, load_meic_session_today
from common import trades_layout
from common.market_hours import session_row_past_0dte_close
from common.trading_gate import evaluate_new_risk_gate, gate_enabled
from meic0dte.app import utilities as util

log = logging.getLogger(__name__)

_GATE_WARN_AT = 0.0
_GATE_WARN_COOLDOWN_SEC = 60.0


def _side_order() -> List[str]:
    raw = os.environ.get('MEIC_ENTRY_SIDE_ORDER', 'P,C')
    return [s.strip().upper() for s in raw.split(',') if s.strip()]


def _stagger_sec() -> float:
    return float(os.environ.get('MEIC_ENTRY_SIDE_STAGGER_SEC', '2.0'))


class EntryMonitorRunner:
    """Poll session CSV and spawn one worker thread per due row."""

    def __init__(
        self,
        *,
        root: Optional[str] = None,
        poll_interval: float = 1.0,
        logger: Optional[logging.Logger] = None,
    ):
        self.root = root
        self.poll_interval = poll_interval
        self.log = logger or log
        self._handles: Dict[str, threading.Thread] = {}
        self._fired: Set[str] = set()
        self._lock = threading.Lock()
        self._lot_side_started_at: Dict[str, float] = {}

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or util.central_now()
        bootstrap_meic_session_if_missing(self.root)
        meic_plan = load_meic_session_today(self.root)
        if meic_plan is not None:
            self._tick_plan(meic_plan, now, manual=False)
        manual_plan = load_manual_session_today(self.root)
        if manual_plan is not None:
            self._tick_plan(manual_plan, now, manual=True)

    def _gate_allows_spawn(self) -> bool:
        global _GATE_WARN_AT
        if not gate_enabled():
            return True
        decision = evaluate_new_risk_gate(require_fresh_probe=True)
        if not decision.blocked:
            return True
        now = time.time()
        if now - _GATE_WARN_AT >= _GATE_WARN_COOLDOWN_SEC:
            _GATE_WARN_AT = now
            self.log.warning(
                'New-risk REST gate blocked entry spawn: %s — %s',
                decision.reason,
                decision.detail,
            )
        return False

    def _stagger_allows(self, row, *, manual: bool) -> bool:
        if manual:
            return True
        order = _side_order()
        side = (row.side or '').upper()
        if side not in order:
            return True
        idx = order.index(side)
        if idx == 0:
            return True
        prev_side = order[idx - 1]
        prev_key = f'{row.lot}_{prev_side}'
        with self._lock:
            if prev_key not in self._fired and prev_key not in self._handles:
                return False
            started = self._lot_side_started_at.get(prev_key)
        if started is None:
            return False
        return (time.monotonic() - started) >= _stagger_sec()

    def _tick_plan(self, plan, now: datetime, *, manual: bool) -> None:
        now_time = now.time()
        strategy = trades_layout.STRATEGY_MANUAL if manual else trades_layout.STRATEGY_MEIC
        for row in plan.rows:
            if manual:
                if not self._should_fire_manual(row, now):
                    continue
            else:
                # Operator may reset failed→pending via session plan window edit.
                with self._lock:
                    if (
                        row.state == 'pending'
                        and row.slot_key in self._fired
                        and row.slot_key not in self._handles
                    ):
                        self._fired.discard(row.slot_key)
                if not self._should_fire_meic(row, now.time(), now):
                    continue
            if not self._stagger_allows(row, manual=manual):
                continue
            if not self._gate_allows_spawn():
                continue
            with self._lock:
                if row.slot_key in self._handles:
                    continue
                if row.slot_key in self._fired:
                    continue
                if manual:
                    if not try_claim_manual_row(plan.path, row.slot_key, strategy=strategy):
                        continue
                else:
                    try:
                        mark_row_entering(plan.path, row.slot_key, strategy=strategy)
                    except KeyError:
                        continue
                self._fired.add(row.slot_key)
                self._lot_side_started_at[row.slot_key] = time.monotonic()
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(plan.path, row.slot_key, manual, strategy),
                    name=f'entry-{row.slot_key}',
                    daemon=True,
                )
                self._handles[row.slot_key] = thread
                thread.start()
                self.log.info('Spawned entry worker for %s (%s)', row.slot_key, strategy)

    def _should_fire_meic(self, row, now_time, now: datetime) -> bool:
        if session_row_past_0dte_close(row, strategy=trades_layout.STRATEGY_MEIC, now=now):
            return False
        return (
            row.state == 'pending'
            and not row.paused
            and not row.skip
            and row.is_in_window(now_time)
        )

    def _should_fire_manual(self, row, now: datetime) -> bool:
        if session_row_past_0dte_close(row, strategy=trades_layout.STRATEGY_MANUAL, now=now):
            return False
        return (
            row.state == 'entering'
            and row.is_manual
            and not row.trade_path
        )

    def _run_worker(self, plan_path: str, slot_key: str, manual: bool, strategy: str) -> None:
        row_log = logging.getLogger(f'entry.{slot_key}')
        result = None
        try:
            from blocks.session.plan import SessionPlan

            plan = SessionPlan.load(plan_path, strategy=strategy)
            row = plan.row_by_slot_key(slot_key)
            if row is None:
                row_log.error('Row %s missing from %s', slot_key, plan_path)
                return
            if manual:
                result = run_manual_entry_row(row, row_log)
            else:
                result = run_meic_entry_row(row, row_log)
        finally:
            with self._lock:
                self._handles.pop(slot_key, None)
            if result is not None:
                apply_entry_result(plan_path, result, strategy=strategy)

    def any_fired(self) -> bool:
        return bool(self._fired)
