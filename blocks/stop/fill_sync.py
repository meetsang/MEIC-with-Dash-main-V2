"""Sync open-order fills from broker into trades/active JSON."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

from brokers.base import BrokerBase, OrderResult
from blocks.stop import state as state_mod
from blocks.stop.fill_provenance import (
    FILL_SYNC_FAST_SEC,
    apply_audit_correction,
    apply_broker_leg_updates,
    can_enter_confirm_pending,
    ensure_fill_sync,
    is_fill_sync_terminal,
    log_order_diagnostics,
    mark_resolved,
    maybe_run_fill_audit,
    schedule_next_poll,
    should_poll_now,
    try_exact_resolution,
    try_protective_estimate,
)
from blocks.stop.stop_math import apply_two_x_thresholds, stop_multiplier_for_state

log = logging.getLogger(__name__)

FILL_SYNC_INTERVAL_SEC = int(FILL_SYNC_FAST_SEC)
PENDING_FILL_SYNC_INTERVAL_SEC = int(FILL_SYNC_FAST_SEC)


def _recompute_stop_fields(state: Dict[str, Any]) -> None:
    mult = stop_multiplier_for_state(state)
    apply_two_x_thresholds(state, mult)


def _promote_open_if_ready(state: Dict[str, Any]) -> None:
    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    filled_qty = int(state.get('filled_quantity') or 0)
    if filled_qty > 0 and short_px > 0 and long_px > 0:
        _recompute_stop_fields(state)
        state['status'] = 'open'


def apply_order_result_to_state(state: Dict[str, Any], result: OrderResult) -> bool:
    """
    Update state from broker open-order status. Returns True if fill qty changed.

    Handshake: entry thread writes JSON with open_order_id; stop_monitor syncs fills here.
  """
    if not result.success:
        return False

    prev_filled = int(state.get('filled_quantity') or 0)
    prev_status = state.get('status')
    prev_short = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    prev_long = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)

    apply_broker_leg_updates(state, result)

    status = (result.status or 'working').lower()
    if status in ('cancelled', 'canceled', 'rejected'):
        state['status'] = 'pending_fill'
        fs = ensure_fill_sync(state)
        fs['phase'] = 'cancelled' if status in ('cancelled', 'canceled') else 'rejected'
        fs['next_poll_epoch'] = None
    elif status == 'expired':
        state['status'] = 'pending_fill'
        fs = ensure_fill_sync(state)
        fs['phase'] = 'expired'
        fs['next_poll_epoch'] = None
    else:
        _promote_open_if_ready(state)
        if state.get('status') != 'open':
            state['status'] = 'pending_fill'

    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    filled_qty = int(state.get('filled_quantity') or 0)
    return (
        filled_qty != prev_filled
        or state.get('status') != prev_status
        or short_px != prev_short
        or long_px != prev_long
    )


def fill_sync_interval_sec(state: Dict[str, Any]) -> int:
    """Pending / unresolved opens poll on the fast interval."""
    if is_fill_sync_terminal(state):
        return PENDING_FILL_SYNC_INTERVAL_SEC
    fs = ensure_fill_sync(state)
    if fs.get('phase') in ('resolved_exact', 'resolved_estimated', 'audit_complete'):
        return PENDING_FILL_SYNC_INTERVAL_SEC
    return PENDING_FILL_SYNC_INTERVAL_SEC


def sync_open_order(
    state: Dict[str, Any],
    broker: BrokerBase,
    *,
    force: bool = False,
    min_interval_sec: Optional[int] = None,
) -> Tuple[bool, Optional[OrderResult]]:
    """Poll broker for open_order_id and update state. Returns (changed, result)."""
    oid = state.get('open_order_id')
    if not oid:
        return False, None

    ensure_fill_sync(state)
    if is_fill_sync_terminal(state):
        from common.broker_cooldown import should_skip_priority
        from common.rest_operations import PRIORITY_LOW

        skip_audit, _ = maybe_run_fill_audit(
            state,
            broker,
            skip_low_priority=should_skip_priority(PRIORITY_LOW),
        )
        if skip_audit:
            _promote_open_if_ready(state)
            return True, None
        return False, None

    open_order = state_mod.section(state, 'open_order')
    fs = ensure_fill_sync(state)

    interval = (
        fill_sync_interval_sec(state)
        if min_interval_sec is None
        else min_interval_sec
    )
    now = time.time()

    if not force and not should_poll_now(state, force=False):
        last = open_order.get('last_sync_epoch') or 0
        if last > 0 and (now - last) < interval:
            return False, None

    phase = fs.get('phase', 'fast')
    confirm_poll = phase == 'confirm_pending'
    if confirm_poll and not fs.get('confirm_attempted'):
        fs['confirm_attempted'] = True

    result = broker.get_order_status(
        str(oid),
        priority='HIGH',
        op='pending_fill_status',
    )
    fs['poll_count'] = int(fs.get('poll_count') or 0) + 1
    open_order['last_sync_epoch'] = now
    log_order_diagnostics(result, lot=str(state.get('lot', '?')))

    if not result.success:
        fs['last_error'] = result.message
        schedule_next_poll(state, delay_sec=interval)
        return False, result

    changed = apply_order_result_to_state(state, result)
    status = (result.status or 'working').lower()

    if status in ('cancelled', 'canceled', 'rejected', 'expired'):
        return changed, result

    if try_exact_resolution(state, result):
        mark_resolved(state, 'resolved_exact', now=now)
        _promote_open_if_ready(state)
        return True, result

    if confirm_poll and fs.get('confirm_attempted'):
        if try_protective_estimate(state, result):
            mark_resolved(state, 'resolved_estimated', now=now)
            _promote_open_if_ready(state)
            return True, result
        fs['phase'] = 'terminal_error'
        fs['next_poll_epoch'] = None
        fs['last_error'] = 'confirm_poll_failed_protective_estimate'
        log.error(
            'Fill sync confirm poll could not resolve lot=%s order=%s',
            state.get('lot'),
            oid,
        )
        return changed, result

    if can_enter_confirm_pending(state, result):
        fs['phase'] = 'confirm_pending'
        fs['confirm_attempted'] = False
        schedule_next_poll(state, delay_sec=interval)
        return changed, result

    if open_order.get('fully_filled'):
        short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
        long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
        if short_px > 0 and long_px > 0:
            mark_resolved(state, 'resolved_exact', now=now)
            _promote_open_if_ready(state)
            return True, result

    schedule_next_poll(state, delay_sec=interval)
    return changed, result


def stop_qty_for_state(state: Dict[str, Any]) -> int:
    """Contracts that should be covered by the exchange stop right now."""
    return int(state.get('filled_quantity') or 0)


def stop_is_current(
    state: Dict[str, Any],
    *,
    ownership_conflict: bool = False,
) -> bool:
    """True when this JSON's active stop quantity matches filled quantity."""
    if ownership_conflict:
        return False
    active = state.get('active_stop') or {}
    if not active.get('order_id'):
        return False
    if active.get('status') in ('filled', 'cancelled', 'rejected', 'expired'):
        return False
    return int(state.get('stop_quantity') or 0) >= stop_qty_for_state(state)


def stop_order_fully_filled(state: Dict[str, Any], result: OrderResult) -> bool:
    """True when the short-leg stop/limit close order fully filled all units."""
    if not result.success:
        return False

    expected = stop_qty_for_state(state)
    active = state.get('active_stop') or {}
    order_qty = int(result.order_quantity or active.get('quantity') or expected or 0)
    if order_qty <= 0:
        order_qty = expected
    filled_qty = int(result.filled_quantity or 0)
    status = str(result.status or '').lower()

    if status == 'filled' and order_qty:
        filled_qty = max(filled_qty, order_qty)
    if order_qty:
        filled_qty = min(filled_qty, order_qty)

    if filled_qty <= 0:
        return False
    if status in ('partial', 'partially filled', 'working', 'live', 'contingent'):
        return False
    return status == 'filled' and filled_qty >= order_qty
