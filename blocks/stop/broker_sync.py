"""Broker stop reconcile — per-tranche refresh in production; repair-only adoption."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Set

from brokers.base import BrokerBase, OrderResult
from blocks.stop import state as state_mod
from blocks.stop.fill_sync import stop_qty_for_state
from blocks.stop.stop_math import exchange_stop_limit_prices, stop_multiplier_for_state
from blocks.stop.stop_ownership import collect_claimed_stop_order_ids
from common.option_ticks import round_spx_option_price

log = logging.getLogger(__name__)

REPAIR_PRICE_TOLERANCE = 0.15
_WORKING_STOP_STATUSES = frozenset(
    ('working', 'live', 'contingent', 'received', 'open', 'partially filled')
)


@dataclass
class RepairCandidate:
    order_id: str
    stop_price: Optional[float]
    limit_price: Optional[float]
    quantity: int


@dataclass
class RepairOutcome:
    status: str
    message: str
    candidate: Optional[RepairCandidate] = None


def expected_exchange_stop_prices(state: dict) -> tuple[float, float]:
    short_fill = float((state.get('short_leg') or {}).get('fill_price') or 0)
    mult = stop_multiplier_for_state(state)
    return exchange_stop_limit_prices(short_fill, mult)


def _order_prices_from_result(result: OrderResult) -> tuple[Optional[float], Optional[float]]:
    stop_price = None
    limit_price = None
    raw = result.raw
    if raw is not None:
        trig = getattr(raw, 'stop_trigger', None)
        if trig is not None:
            stop_price = round_spx_option_price(float(trig))
        if getattr(raw, 'price', None) is not None:
            limit_price = round_spx_option_price(abs(float(raw.price)))
    return stop_price, limit_price


def _stop_prices_compatible(
    expected_stop: float,
    expected_limit: float,
    actual_stop: Optional[float],
    actual_limit: Optional[float],
    tolerance: float = REPAIR_PRICE_TOLERANCE,
) -> bool:
    if actual_stop is None:
        return False
    if abs(float(actual_stop) - float(expected_stop)) > tolerance:
        return False
    if actual_limit is not None:
        if abs(float(actual_limit) - float(expected_limit)) > tolerance + 0.05:
            return False
    return True


def refresh_own_active_stop_with_broker(state: dict, broker: BrokerBase) -> bool:
    """
    Production slow sync: refresh status for this JSON's active_stop.order_id only.

  Never adopts another tranche's working BTC on the same symbol.
    """
    if str(state.get('status') or '') != 'open':
        return False

    active = state.get('active_stop') or {}
    oid = str(active.get('order_id') or '')
    if not oid:
        return False

    result = broker.get_order_status(oid)
    if not result.success:
        return False

    st = str(result.status).lower()
    if st in _WORKING_STOP_STATUSES:
        active['status'] = 'working'
        return True
    if st == 'filled':
        active['status'] = 'filled'
        return True
    if st in ('cancelled', 'canceled', 'rejected', 'expired', 'unknown'):
        active['status'] = st
        return True

    active['status'] = result.status
    return True


def clear_terminal_own_stop(state: dict) -> bool:
    """Clear active_stop when this JSON's order is terminal — enables per-tranche replace."""
    active = state.get('active_stop') or {}
    oid = str(active.get('order_id') or '')
    if not oid:
        return False
    st = str(active.get('status') or '').lower()
    if st not in ('cancelled', 'canceled', 'rejected', 'expired', 'unknown'):
        return False
    state['active_stop'] = None
    state['stop_quantity'] = 0
    return True


def find_repair_candidates(
    state: dict,
    broker: BrokerBase,
    *,
    claimed_order_ids: Optional[Set[str]] = None,
    price_tolerance: float = REPAIR_PRICE_TOLERANCE,
) -> List[RepairCandidate]:
    """Strict orphan-stop matching for explicit repair only."""
    if state_mod.section(state, 'active_stop').get('order_id'):
        return []
    if str(state.get('status') or '') != 'open':
        return []
    required_qty = stop_qty_for_state(state)
    if required_qty <= 0:
        return []

    short_sym = (state.get('short_leg') or {}).get('symbol')
    if not short_sym:
        return []

    claimed = claimed_order_ids if claimed_order_ids is not None else set(
        collect_claimed_stop_order_ids().keys()
    )
    expected_stop, expected_limit = expected_exchange_stop_prices(state)

    finder = getattr(broker, 'find_working_close_orders', None)
    if not finder:
        return []

    candidates: List[RepairCandidate] = []
    for result in finder(short_sym):
        if not isinstance(result, OrderResult) or not result.success or not result.order_id:
            continue
        oid = str(result.order_id)
        if oid in claimed:
            continue

        raw = result.raw
        order_type = str(getattr(raw, 'order_type', 'Stop Limit') if raw else 'Stop Limit').lower()
        if 'stop' not in order_type and order_type != 'limit':
            continue

        qty = int(result.order_quantity or result.filled_quantity or 0)
        if qty != required_qty:
            continue

        stop_price, limit_price = _order_prices_from_result(result)
        if not _stop_prices_compatible(
            expected_stop, expected_limit, stop_price, limit_price, price_tolerance,
        ):
            continue

        candidates.append(
            RepairCandidate(
                order_id=oid,
                stop_price=stop_price,
                limit_price=limit_price,
                quantity=qty,
            )
        )
    return candidates


def repair_orphan_stop(
    state: dict,
    broker: BrokerBase,
    *,
    apply: bool = False,
    repair_reason: str = 'explicit_repair',
    spx_price: Optional[float] = None,
    claimed_order_ids: Optional[Set[str]] = None,
    price_tolerance: float = REPAIR_PRICE_TOLERANCE,
) -> RepairOutcome:
    """
    Repair-only adoption. Default dry-run (apply=False).

    Refuses ambiguous matches (multiple candidates).
    """
    if state_mod.section(state, 'active_stop').get('order_id'):
        return RepairOutcome('already_has_stop', 'JSON already has active_stop.order_id')

    candidates = find_repair_candidates(
        state,
        broker,
        claimed_order_ids=claimed_order_ids,
        price_tolerance=price_tolerance,
    )
    if not candidates:
        return RepairOutcome('no_candidate', 'No unique qty/price-compatible orphan stop')
    if len(candidates) > 1:
        ids = [c.order_id for c in candidates]
        log.warning('AMBIGUOUS_REPAIR: multiple broker stops match: %s', ids)
        return RepairOutcome(
            'ambiguous',
            f'AMBIGUOUS_REPAIR: {len(candidates)} broker orders match — operator must choose',
        )

    cand = candidates[0]
    if not apply:
        return RepairOutcome(
            'dry_run',
            f'Would adopt orphan stop {cand.order_id} qty={cand.quantity}',
            candidate=cand,
        )

    state['active_stop'] = {
        'order_id': cand.order_id,
        'type': 'STOP_LIMIT',
        'stop_price': cand.stop_price,
        'limit_price': cand.limit_price,
        'phase': 1,
        'status': 'working',
        'placed_at': state_mod.now_iso(),
        'quantity': cand.quantity,
        'adopted_from_broker': True,
        'repair_mode': True,
        'repair_reason': repair_reason,
        'repaired_at': state_mod.now_iso(),
    }
    state['stop_quantity'] = cand.quantity
    if cand.stop_price is not None:
        state['designated_stop_price'] = float(cand.stop_price)

    spx_val = spx_price if isinstance(spx_price, (int, float)) else None
    state_mod.append_stop_history(
        state,
        action='adopted',
        order_id=cand.order_id,
        price=cand.stop_price or cand.limit_price,
        phase=1,
        reason=repair_reason,
        spx_price_at_event=spx_val,
    )
    log.info(
        'Repair adopted broker stop %s for short %s qty=%s',
        cand.order_id,
        (state.get('short_leg') or {}).get('symbol'),
        cand.quantity,
    )
    return RepairOutcome(
        'adopted',
        f'Adopted orphan stop {cand.order_id}',
        candidate=cand,
    )


def adopt_active_stop_from_broker(
    state: dict,
    broker: BrokerBase,
    *,
    spx_price: Optional[float] = None,
    apply: bool = True,
) -> bool:
    """
    Deprecated for production — use repair_orphan_stop(apply=True) from explicit CLI only.
    """
    outcome = repair_orphan_stop(
        state,
        broker,
        apply=apply,
        repair_reason='adopted_existing_broker_stop',
        spx_price=spx_price,
    )
    return outcome.status == 'adopted'


def cancel_all_close_orders_on_short(state: dict, broker: BrokerBase) -> int:
    """
    Cancel every live BTC on the short symbol.

    Repair/clean-slate only — not used by breach, manual kill, or Kill All exit paths.
    """
    finder = getattr(broker, 'find_working_close_orders', None)
    if not finder:
        return 0
    short_sym = (state.get('short_leg') or {}).get('symbol')
    if not short_sym:
        return 0
    cancelled = 0
    for result in finder(short_sym):
        oid = str(result.order_id or '')
        if not oid:
            continue
        broker.cancel_order(oid)
        cancelled += 1
        log.info('Cancelled broker close order %s on %s (repair)', oid, short_sym)
    return cancelled
