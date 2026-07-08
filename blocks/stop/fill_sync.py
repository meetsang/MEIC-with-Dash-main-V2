"""Sync open-order fills from broker into trades/active JSON."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from brokers.base import BrokerBase, OrderResult
from blocks.stop import state as state_mod
from blocks.stop.stop_math import apply_two_x_thresholds, stop_multiplier_for_state

log = logging.getLogger(__name__)

FILL_SYNC_INTERVAL_SEC = 3
PENDING_FILL_SYNC_INTERVAL_SEC = 3


def _recompute_stop_fields(state: Dict[str, Any]) -> None:
    mult = stop_multiplier_for_state(state)
    apply_two_x_thresholds(state, mult)


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
    order_qty = int(result.order_quantity or state.get('quantity') or 0)
    filled_qty = int(result.filled_quantity or 0)
    status = (result.status or 'working').lower()

    if status == 'filled' and order_qty:
        filled_qty = max(filled_qty, order_qty)
    filled_qty = min(filled_qty, order_qty) if order_qty else filled_qty

    state['quantity'] = order_qty or state.get('quantity', 0)
    state['filled_quantity'] = filled_qty
    state.setdefault('open_order', {})
    state['open_order']['status'] = status
    state['open_order']['last_sync'] = state_mod.now_iso()

    if result.filled_price is not None and filled_qty > 0:
        state['entry']['net_credit'] = round(float(result.filled_price), 2)

    short_fill = getattr(result, 'short_fill_price', None)
    long_fill = getattr(result, 'long_fill_price', None)
    if short_fill is not None:
        state['short_leg']['fill_price'] = round(float(short_fill), 2)
    if long_fill is not None:
        state['long_leg']['fill_price'] = round(float(long_fill), 2)

    short_px = float(state['short_leg'].get('fill_price') or 0)
    long_px = float(state['long_leg'].get('fill_price') or 0)
    spread_complete = filled_qty > 0 and short_px > 0 and long_px > 0
    fully_filled = bool(order_qty and filled_qty >= order_qty and status == 'filled')

    if spread_complete:
        _recompute_stop_fields(state)
        # 'open' with partial qty so stop covers filled units; fully_filled gates entry sync stop.
        state['status'] = 'open'
    elif status in ('cancelled', 'canceled', 'rejected'):
        state['status'] = 'pending_fill'
    else:
        state['status'] = 'pending_fill'

    state['open_order']['fully_filled'] = fully_filled
    return (
        filled_qty != prev_filled
        or state.get('status') != prev_status
        or short_px != prev_short
        or long_px != prev_long
    )


def fill_sync_interval_sec(state: Dict[str, Any]) -> int:
    """Pending / unfilled opens poll faster so stops follow dashboard reprices quickly."""
    if state.get('status') == 'pending_fill':
        return PENDING_FILL_SYNC_INTERVAL_SEC
    open_order = state_mod.section(state, 'open_order')
    if not open_order.get('fully_filled'):
        return PENDING_FILL_SYNC_INTERVAL_SEC
    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    if short_px <= 0 or long_px <= 0:
        return PENDING_FILL_SYNC_INTERVAL_SEC
    return FILL_SYNC_INTERVAL_SEC


def sync_open_order(
    state: Dict[str, Any],
    broker: BrokerBase,
    *,
    force: bool = False,
    min_interval_sec: Optional[int] = None,
) -> Tuple[bool, Optional[OrderResult]]:
    """Poll broker for open_order_id and update state. Returns (changed, result)."""
    import time

    oid = state.get('open_order_id')
    if not oid:
        return False, None

    open_order = state_mod.section(state, 'open_order')
    if open_order.get('fully_filled'):
        short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
        long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
        if short_px > 0 and long_px > 0:
            return False, None

    interval = (
        fill_sync_interval_sec(state)
        if min_interval_sec is None
        else min_interval_sec
    )
    last = open_order.get('last_sync_epoch') or 0
    now = time.time()
    if not force and last > 0 and (now - last) < interval:
        return False, None

    result = broker.get_order_status(str(oid))
    changed = apply_order_result_to_state(state, result)
    state['open_order']['last_sync_epoch'] = now
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
