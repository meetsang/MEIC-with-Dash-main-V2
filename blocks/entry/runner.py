"""Supervisor for MEIC and manual session entry workers."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Dict, Optional, Set

from blocks.entry.manual_worker import run_manual_entry_row
from blocks.entry.meic_worker import run_meic_entry_row
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.csv_update import (
    apply_entry_result,
    mark_row_entering,
    mark_row_state,
    try_claim_manual_row,
)
from blocks.session.plan import load_manual_session_today, load_meic_session_today
from common import trades_layout
from common.market_hours import session_row_past_0dte_close
from common.portfolio_guard import is_strategy_halted
from common.probe_coordinator import get_coordinator
from common.trading_gate import evaluate_new_risk_gate, gate_enabled
from meic0dte.app import utilities as util

log = logging.getLogger(__name__)

_GATE_WARN_AT = 0.0
_GATE_WARN_COOLDOWN_SEC = 60.0


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
        self._missed: Set[str] = set()
        self._lock = threading.Lock()
        self._manual_probe_requested: Set[str] = set()

    def tick(self, now: Optional[datetime] = None) -> None:
        now = now or util.central_now()
        bootstrap_meic_session_if_missing(self.root)
        meic_plan = load_meic_session_today(self.root)
        if meic_plan is not None:
            # Miss detection before spawn path — must run even when CSV spawn I/O is slow.
            self._emit_tranche_missed(meic_plan, now)
            self._tick_plan(meic_plan, now, manual=False)
        manual_plan = load_manual_session_today(self.root)
        if manual_plan is not None:
            self._tick_plan(manual_plan, now, manual=True)

    def _gate_allows_spawn(
        self,
        *,
        strategy: str = '',
        tranche_id: Optional[str] = None,
        manual: bool = False,
        plan_path: Optional[str] = None,
        slot_key: Optional[str] = None,
    ) -> bool:
        global _GATE_WARN_AT
        if not gate_enabled():
            return True
        # For manual entries, check global REST readiness first; only require a per-slot probe if global is stale.
        if manual:
            decision = evaluate_new_risk_gate(
                require_fresh_probe=True,
                strategy=strategy,
                tranche_id=None,
            )
            if decision.blocked:
                decision = evaluate_new_risk_gate(
                    require_fresh_probe=True,
                    strategy=strategy,
                    tranche_id=tranche_id,
                )
        else:
            decision = evaluate_new_risk_gate(
                require_fresh_probe=True,
                strategy=strategy,
                tranche_id=tranche_id,
            )
        if not decision.blocked:
            if manual and plan_path and slot_key:
                # Clear waiting/blocked banner states once probe passes.
                try:
                    mark_row_state(plan_path, slot_key, state='entering', strategy=strategy)
                except Exception:
                    pass
            return True
        if manual and plan_path and slot_key:
            self._update_manual_gate_state(
                plan_path=plan_path,
                slot_key=slot_key,
                strategy=strategy,
                decision=decision,
            )
        elif not manual and tranche_id:
            reason = str(decision.reason or '')
            if reason in ('rest_probe_pending', 'rest_probe_missing'):
                self._ensure_tranche_probe(tranche_id=tranche_id, strategy=strategy or trades_layout.STRATEGY_MEIC)
        now = time.time()
        if now - _GATE_WARN_AT >= _GATE_WARN_COOLDOWN_SEC:
            _GATE_WARN_AT = now
            self.log.warning(
                'New-risk REST gate blocked entry spawn: %s — %s',
                decision.reason,
                decision.detail,
            )
        return False

    def _ensure_tranche_probe(self, *, tranche_id: str, strategy: str) -> None:
        coord = get_coordinator()
        if coord is not None:
            coord.schedule_tranche_probe(strategy=strategy, tranche_id=tranche_id, source='pre_tranche')

    def _update_manual_gate_state(self, *, plan_path, slot_key, strategy, decision) -> None:
        reason = str(decision.reason or '')
        detail = str(decision.detail or '')
        if reason in ('rest_probe_pending', 'rest_probe_missing'):
            # Ensure exactly one async probe is scheduled for this manual slot.
            self._ensure_manual_probe(slot_key=slot_key, strategy=strategy)
            try:
                mark_row_state(
                    plan_path,
                    slot_key,
                    state='waiting_rest_probe',
                    strategy=strategy,
                    error=detail or reason,
                )
            except Exception:
                pass
            return
        try:
            mark_row_state(
                plan_path,
                slot_key,
                state='blocked_rest',
                strategy=strategy,
                error=detail or reason,
            )
        except Exception:
            pass

    def _ensure_manual_probe(self, *, slot_key: str, strategy: str) -> None:
        with self._lock:
            if slot_key in self._manual_probe_requested:
                return
            self._manual_probe_requested.add(slot_key)
        coord = get_coordinator()
        if coord is None:
            self.log.warning(
                'No ProbeCoordinator — cannot schedule manual REST probe for %s',
                slot_key,
            )
            return
        scheduled = coord.schedule_manual_probe(slot_key=slot_key, strategy=strategy)
        if not scheduled:
            self.log.info('Manual REST probe already scheduled/completed for %s', slot_key)

    def _prepare_meic_tranche_chain(self, row) -> bool:
        try:
            from blocks.entry.tranche_chain import prepare_meic_tranche_chain
            from common.broker_factory import get_shared_broker
            from meic0dte.app import utilities as util

            broker = get_shared_broker()
            expiry = util.get_expiration_date(self.log)
            credit_min = float(row.credit_min or 0.60)
            prepare_meic_tranche_chain(
                broker,
                expiry,
                row.lot,
                self.log,
                credit_min=credit_min,
            )
            return True
        except Exception as exc:
            self.log.error(
                'MEIC tranche chain prep failed lot=%s: %s',
                row.lot,
                exc,
            )
            return False

    def _tick_plan(self, plan, now: datetime, *, manual: bool) -> None:
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
                if not self._prepare_meic_tranche_chain(row):
                    continue
            # Manual: probe identity is slot_key; MEIC: tranche lot shared by P/C
            tranche_id = row.slot_key if manual else row.lot
            if not self._gate_allows_spawn(
                strategy=strategy,
                tranche_id=tranche_id,
                manual=manual,
                plan_path=plan.path,
                slot_key=row.slot_key if manual else None,
            ):
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
            with self._lock:
                if row.slot_key in self._handles:
                    continue
                if row.slot_key in self._fired:
                    continue
                self._fired.add(row.slot_key)
                thread = threading.Thread(
                    target=self._run_worker,
                    args=(plan.path, row.slot_key, manual, strategy),
                    name=f'entry-{row.slot_key}',
                    daemon=True,
                )
                self._handles[row.slot_key] = thread
                thread.start()
                self.log.info('Spawned entry worker for %s (%s)', row.slot_key, strategy)

    def _emit_tranche_missed(self, plan, now: datetime) -> None:
        """Exactly-once CRITICAL when a pending MEIC window has ended with no spawn."""
        from common.meic_settings import is_meic_side_enabled
        now_time = now.time()
        for row in plan.rows:
            if row.state != 'pending' or row.paused or row.skip or not is_meic_side_enabled(row.side, root=self.root):
                continue
            if session_row_past_0dte_close(row, strategy=trades_layout.STRATEGY_MEIC, now=now):
                continue
            try:
                end = row.window_end_time()
            except Exception:
                continue
            if now_time <= end:
                continue
            with self._lock:
                if row.slot_key in self._fired or row.slot_key in self._missed:
                    continue
                self._missed.add(row.slot_key)
            reason = 'unknown'
            try:
                decision = evaluate_new_risk_gate(
                    require_fresh_probe=True,
                    strategy=trades_layout.STRATEGY_MEIC,
                    tranche_id=row.lot,
                )
                if decision.blocked:
                    reason = decision.reason or 'gate_blocked'
            except Exception:
                reason = 'gate_eval_error'
            self.log.critical(
                'TRANCHE_MISSED slot=%s window=%s-%s reason=%s',
                row.slot_key,
                row.entry_window_start,
                row.entry_window_end,
                reason,
            )

    def _should_fire_meic(self, row, now_time, now: datetime) -> bool:
        if is_strategy_halted(trades_layout.STRATEGY_MEIC, self.root):
            return False
        if session_row_past_0dte_close(row, strategy=trades_layout.STRATEGY_MEIC, now=now):
            return False
        from common.meic_settings import is_meic_side_enabled
        if not is_meic_side_enabled(row.side, root=self.root):
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
        # Include waiting/blocked so ticks keep evaluating after probe completes
        return (
            row.state in ('entering', 'waiting_rest_probe', 'blocked_rest')
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
