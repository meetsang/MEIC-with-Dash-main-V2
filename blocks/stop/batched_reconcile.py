"""Batched peaceful working-stop reconcile around one live-orders snapshot."""
from __future__ import annotations

import logging
from typing import Any, Iterable, List, Optional, Sequence

from common.rest_operations import OPERATION_WORKING_STOP_RECONCILE, PRIORITY_LOW

log = logging.getLogger(__name__)


def fetch_live_orders_snapshot(broker) -> list:
    """One uncached get_live_orders for the current reconcile cycle."""
    if hasattr(broker, 'get_live_orders_cached'):
        orders = broker.get_live_orders_cached(ttl_sec=0)
        if hasattr(broker, 'prime_live_orders_cache'):
            broker.prime_live_orders_cache(orders)
        return list(orders or [])
    return []


def reconcile_active_stop_with_snapshot(
    monitor,
    *,
    live_orders: Optional[Sequence[Any]] = None,
) -> None:
    """Reconcile one monitor's active_stop using a shared snapshot when provided."""
    if hasattr(monitor, '_reconcile_active_stop_with_broker'):
        monitor._reconcile_active_stop_with_broker(live_orders=live_orders)


def batch_peaceful_reconcile(
    broker,
    monitors: Iterable[Any],
    *,
    skip: bool = False,
) -> list:
    """Run peaceful reconcile for many monitors with one shared live-orders snapshot.

    Returns the snapshot used (empty when skipped or on failure).
    Direct get_order is used only for unresolved order ids absent from the snapshot
    (via broker.get_order_status(..., live_orders=snapshot)).
    """
    monitors = [m for m in monitors if m is not None]
    if not monitors or skip:
        return []
    try:
        snapshot = fetch_live_orders_snapshot(broker)
    except Exception:
        log.exception('batched peaceful reconcile snapshot failed')
        snapshot = None
    for mon in monitors:
        try:
            reconcile_active_stop_with_snapshot(mon, live_orders=snapshot)
            if hasattr(mon, '_sync_working_stop_order'):
                mon._sync_working_stop_order()
        except Exception:
            log.exception('batched reconcile failed for monitor')
    return list(snapshot or [])
