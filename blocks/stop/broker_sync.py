"""Reconcile trades/active JSON with live broker close orders on the short leg."""
from __future__ import annotations

import logging
from typing import Any, Optional

from brokers.base import BrokerBase, OrderResult
from blocks.stop import state as state_mod

log = logging.getLogger(__name__)


def adopt_active_stop_from_broker(
    state: dict,
    broker: BrokerBase,
    *,
    spx_price: Optional[float] = None,
) -> bool:
    """
    Manual repair only (adhoc sync-broker-stop): link JSON to a live broker stop.

    Not used by stop_monitor in production — each tranche places its own stop.
    """
    if state_mod.section(state, 'active_stop').get('order_id'):
        return True

    finder = getattr(broker, 'find_working_close_order', None)
    if not finder:
        return False

    result = finder(state['short_leg']['symbol'])
    if not isinstance(result, OrderResult) or not result.success or not result.order_id:
        return False

    order = result.raw
    if order is None:
        return False

    order_type = str(getattr(order, 'order_type', 'Stop Limit')).lower()
    stop_price = None
    limit_price = None
    if getattr(order, 'stop_trigger', None) is not None:
        stop_price = round(float(order.stop_trigger), 2)
    if getattr(order, 'price', None) is not None:
        limit_price = round(abs(float(order.price)), 2)

    qty = int(result.order_quantity or result.filled_quantity or state.get('quantity') or 0)
    status = str(getattr(order, 'status', 'working')).lower()
    if status == 'live':
        status = 'working'

    state['active_stop'] = {
        'order_id': str(result.order_id),
        'type': 'STOP_LIMIT' if 'stop' in order_type else 'LIMIT',
        'stop_price': stop_price,
        'limit_price': limit_price,
        'phase': 1,
        'status': status,
        'placed_at': state_mod.now_iso(),
        'quantity': qty,
        'adopted_from_broker': True,
    }
    state['stop_quantity'] = qty

    spx_val = spx_price if isinstance(spx_price, (int, float)) else None
    state_mod.append_stop_history(
        state,
        action='adopted',
        order_id=str(result.order_id),
        price=stop_price or limit_price,
        phase=1,
        reason='adopted_existing_broker_stop',
        spx_price_at_event=spx_val,
    )
    log.info('Adopted broker stop %s for short %s', result.order_id, state['short_leg']['symbol'])
    return True


def cancel_all_close_orders_on_short(state: dict, broker: BrokerBase) -> int:
    """Cancel every live BUY TO CLOSE on the short leg. Returns count cancelled."""
    finder = getattr(broker, 'find_working_close_orders', None)
    if not finder:
        return 0
    cancelled = 0
    for result in finder(state['short_leg']['symbol']):
        oid = str(result.order_id or '')
        if not oid:
            continue
        broker.cancel_order(oid)
        cancelled += 1
        log.info('Cancelled broker close order %s on %s', oid, state['short_leg']['symbol'])
    return cancelled
