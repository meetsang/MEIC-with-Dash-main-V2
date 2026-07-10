"""Fill provenance, protective estimate, and bounded fill-sync state helpers."""
from __future__ import annotations

import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple

from brokers.base import OrderResult
from blocks.stop import state as state_mod
from common.option_ticks import round_spx_option_price

log = logging.getLogger(__name__)

FILL_SYNC_SCHEMA_VERSION = 2
FILL_SYNC_FAST_SEC = float(os.environ.get('FILL_SYNC_FAST_SEC', '3'))
FILL_ESTIMATE_AUDIT_DELAY_SEC = float(os.environ.get('FILL_ESTIMATE_AUDIT_DELAY_SEC', '60'))
FILL_IDENTITY_TOLERANCE = float(os.environ.get('FILL_IDENTITY_TOLERANCE', '0.02'))
FILL_INFERENCE_MIN_PRICE = float(os.environ.get('FILL_INFERENCE_MIN_PRICE', '0.00'))
FILL_INFERENCE_MAX_SPREAD_OVER_WIDTH = float(
    os.environ.get('FILL_INFERENCE_MAX_SPREAD_OVER_WIDTH', '0.05')
)

TERMINAL_PHASES = frozenset({
    'resolved_exact',
    'resolved_estimated',
    'audit_complete',
    'cancelled',
    'rejected',
    'expired',
    'terminal_error',
})

SOURCE_BROKER_LEG = 'broker_leg_fill'
SOURCE_PROTECTIVE_SHORT = 'protective_estimate_from_limit'
SOURCE_PROTECTIVE_LONG = 'protective_estimate_from_limit'
SOURCE_ORDER_LIMIT = 'order_limit'
CONFIDENCE_EXACT = 'exact'
CONFIDENCE_PROTECTIVE = 'protective_estimate'
CONFIDENCE_UNKNOWN = 'unknown'


def ensure_fill_sync(state: Dict[str, Any]) -> Dict[str, Any]:
    open_order = state_mod.section(state, 'open_order')
    fs = open_order.get('fill_sync')
    if not isinstance(fs, dict):
        fs = {
            'schema_version': FILL_SYNC_SCHEMA_VERSION,
            'phase': 'fast',
            'poll_count': 0,
            'confirm_attempted': False,
            'trusted_value_count': 0,
            'inferred': False,
            'resolved_at_epoch': None,
            'next_poll_epoch': 0.0,
            'audit_due_epoch': None,
            'audit_attempted': False,
            'last_error': None,
        }
        open_order['fill_sync'] = fs
    return fs


def is_fill_sync_terminal(state: Dict[str, Any]) -> bool:
    fs = (state.get('open_order') or {}).get('fill_sync') or {}
    phase = fs.get('phase', 'fast')
    if phase not in TERMINAL_PHASES:
        return False
    if phase in ('cancelled', 'rejected', 'expired', 'terminal_error'):
        return True
    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    return short_px > 0 and long_px > 0


def spread_width_points(state: Dict[str, Any]) -> float:
    short_strike = float(state_mod.section(state, 'short_leg').get('strike') or 0)
    long_strike = float(state_mod.section(state, 'long_leg').get('strike') or 0)
    return abs(short_strike - long_strike)


def limit_credit(state: Dict[str, Any]) -> Optional[float]:
    entry = state_mod.section(state, 'entry')
    for key in ('limit_credit', 'net_credit'):
        val = entry.get(key)
        if val is not None:
            try:
                return round(float(val), 2)
            except (TypeError, ValueError):
                continue
    return None


def _quantize_leg(price: float) -> float:
    return round_spx_option_price(price)


def _append_fill_history(state: Dict[str, Any], event: Dict[str, Any]) -> None:
    history = state.setdefault('fill_history', [])
    if not isinstance(history, list):
        history = []
        state['fill_history'] = history
    event = dict(event)
    event.setdefault('timestamp', state_mod.now_iso())
    history.append(event)


def _set_fill_credit(
    state: Dict[str, Any],
    credit: float,
    *,
    source: str,
    confidence: str,
) -> None:
    entry = state_mod.section(state, 'entry')
    rounded = round(float(credit), 2)
    entry['fill_credit'] = rounded
    entry['fill_credit_source'] = source
    entry['fill_confidence'] = confidence
    entry['net_credit'] = rounded


def _broker_leg_count(result: OrderResult) -> int:
    count = 0
    if result.short_fill_price is not None:
        count += 1
    if result.long_fill_price is not None:
        count += 1
    return count


def _is_genuine_aggregate(result: OrderResult) -> bool:
    return result.filled_price_source == 'broker_aggregate_execution'


def apply_broker_leg_updates(state: Dict[str, Any], result: OrderResult) -> None:
    """Apply broker-reported leg fills without inferring execution credit from limit."""
    short_fill = result.short_fill_price
    long_fill = result.long_fill_price
    if short_fill is not None:
        px = round(float(short_fill), 2)
        state['short_leg']['fill_price'] = px
        state['short_leg']['fill_price_source'] = SOURCE_BROKER_LEG
    if long_fill is not None:
        px = round(float(long_fill), 2)
        state['long_leg']['fill_price'] = px
        state['long_leg']['fill_price_source'] = SOURCE_BROKER_LEG

    order_qty = int(result.order_quantity or state.get('quantity') or 0)
    filled_qty = int(result.filled_quantity or 0)
    status = (result.status or 'working').lower()
    if status == 'filled' and order_qty:
        filled_qty = max(filled_qty, order_qty)
    if order_qty:
        filled_qty = min(filled_qty, order_qty)

    state['quantity'] = order_qty or state.get('quantity', 0)
    state['filled_quantity'] = filled_qty
    open_order = state_mod.section(state, 'open_order')
    open_order['status'] = status
    open_order['last_sync'] = state_mod.now_iso()
    open_order['fully_filled'] = bool(
        order_qty and filled_qty >= order_qty and status == 'filled'
    )

    if (
        short_fill is not None
        and long_fill is not None
        and filled_qty > 0
        and (
            result.filled_price_source in ('broker_leg_math', 'broker_aggregate_execution')
            or result.filled_price_source is None
        )
    ):
        credit = round(float(short_fill) - float(long_fill), 2)
        source = result.filled_price_source or 'broker_leg_math'
        _set_fill_credit(
            state,
            credit,
            source=source,
            confidence=CONFIDENCE_EXACT,
        )
    elif (
        result.filled_price is not None
        and result.filled_price_source in ('broker_leg_math', 'broker_aggregate_execution')
        and filled_qty > 0
    ):
        _set_fill_credit(
            state,
            float(result.filled_price),
            source=result.filled_price_source,
            confidence=CONFIDENCE_EXACT,
        )


def try_exact_resolution(state: Dict[str, Any], result: OrderResult) -> bool:
    short_px = state_mod.section(state, 'short_leg').get('fill_price')
    long_px = state_mod.section(state, 'long_leg').get('fill_price')
    if short_px is not None and long_px is not None:
        s = float(short_px)
        l = float(long_px)
        if s > 0 and l > 0:
            credit = round(s - l, 2)
            _set_fill_credit(
                state,
                credit,
                source='broker_leg_math',
                confidence=CONFIDENCE_EXACT,
            )
            return True

    limit = limit_credit(state)
    agg = result.broker_aggregate_fill_price
    if agg is not None and _is_genuine_aggregate(result):
        agg_f = float(agg)
        if result.short_fill_price is not None and result.long_fill_price is None:
            inferred_long = _quantize_leg(float(result.short_fill_price) - agg_f)
            if inferred_long >= FILL_INFERENCE_MIN_PRICE:
                state['long_leg']['fill_price'] = inferred_long
                state['long_leg']['fill_price_source'] = SOURCE_BROKER_LEG
                _set_fill_credit(state, agg_f, source='broker_aggregate_execution', confidence=CONFIDENCE_EXACT)
                return True
        if result.long_fill_price is not None and result.short_fill_price is None:
            inferred_short = _quantize_leg(float(result.long_fill_price) + agg_f)
            state['short_leg']['fill_price'] = inferred_short
            state['short_leg']['fill_price_source'] = SOURCE_BROKER_LEG
            _set_fill_credit(state, agg_f, source='broker_aggregate_execution', confidence=CONFIDENCE_EXACT)
            return True

    if (
        result.filled_price is not None
        and result.filled_price_source == 'broker_leg_math'
        and _broker_leg_count(result) == 2
    ):
        _set_fill_credit(
            state,
            float(result.filled_price),
            source='broker_leg_math',
            confidence=CONFIDENCE_EXACT,
        )
        return True

    return False


def _validate_inferred_spread(
    short_px: float,
    long_px: float,
    credit: float,
    state: Dict[str, Any],
) -> bool:
    if long_px < FILL_INFERENCE_MIN_PRICE:
        return False
    if credit < -FILL_IDENTITY_TOLERANCE:
        return False
    spread = round(short_px - long_px, 2)
    if spread < -FILL_IDENTITY_TOLERANCE:
        return False
    width = spread_width_points(state)
    if width > 0 and spread > width + FILL_INFERENCE_MAX_SPREAD_OVER_WIDTH:
        return False
    return True


def try_protective_estimate(state: Dict[str, Any], result: OrderResult) -> bool:
    open_order = state_mod.section(state, 'open_order')
    if not open_order.get('fully_filled'):
        return False

    order_qty = int(state.get('quantity') or 0)
    filled_qty = int(state.get('filled_quantity') or 0)
    if not order_qty or filled_qty < order_qty:
        return False

    status = (result.status or '').lower()
    if status in ('cancelled', 'canceled', 'rejected', 'expired', 'working', 'partial'):
        return False

    if _is_genuine_aggregate(result):
        return False

    limit = limit_credit(state)
    if limit is None:
        return False

    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    short_src = state_mod.section(state, 'short_leg').get('fill_price_source')
    long_src = state_mod.section(state, 'long_leg').get('fill_price_source')

    has_short = short_px > 0 and short_src == SOURCE_BROKER_LEG
    has_long = long_px > 0 and long_src == SOURCE_BROKER_LEG
    if has_short == has_long:
        return False

    inferred_field = None
    inferred_px = None
    basis: Dict[str, Any] = {'limit_credit': limit}

    if has_short:
        raw_long = Decimal(str(short_px)) - Decimal(str(limit))
        inferred_px = _quantize_leg(float(raw_long))
        inferred_field = 'long_leg.fill_price'
        basis['short_fill'] = short_px
        final_short, final_long = short_px, inferred_px
    else:
        raw_short = Decimal(str(long_px)) + Decimal(str(limit))
        inferred_px = _quantize_leg(float(raw_short))
        inferred_field = 'short_leg.fill_price'
        basis['long_fill'] = long_px
        final_short, final_long = inferred_px, long_px

    credit = round(final_short - final_long, 2)

    if not _validate_inferred_spread(final_short, final_long, credit, state):
        log.warning(
            'Protective estimate rejected lot=%s short=%s long=%s credit=%s',
            state.get('lot'),
            final_short,
            final_long,
            credit,
        )
        return False

    if inferred_field == 'long_leg.fill_price':
        state['long_leg']['fill_price'] = inferred_px
        state['long_leg']['fill_price_source'] = SOURCE_PROTECTIVE_LONG
    else:
        state['short_leg']['fill_price'] = inferred_px
        state['short_leg']['fill_price_source'] = SOURCE_PROTECTIVE_SHORT

    _set_fill_credit(
        state,
        credit,
        source='protective_estimate',
        confidence=CONFIDENCE_PROTECTIVE,
    )

    fs = ensure_fill_sync(state)
    fs['inferred'] = True
    fs['inferred_field'] = inferred_field
    fs['inference_basis'] = [
        f"short_leg.fill_price:{short_src or 'missing'}",
        f"long_leg.fill_price:{long_src or 'missing'}",
        'entry.limit_credit:order_limit',
    ]
    fs['trusted_value_count'] = 1

    _append_fill_history(state, {
        'action': 'protective_inference',
        'field': inferred_field,
        'value': inferred_px,
        'basis': basis,
    })
    return True


def can_enter_confirm_pending(state: Dict[str, Any], result: OrderResult) -> bool:
    open_order = state_mod.section(state, 'open_order')
    if not open_order.get('fully_filled'):
        return False
    if limit_credit(state) is None:
        return False
    if _broker_leg_count(result) != 1:
        return False
    if _is_genuine_aggregate(result):
        return False
    short_px = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
    long_px = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
    return (short_px > 0) ^ (long_px > 0)


def mark_resolved(
    state: Dict[str, Any],
    phase: str,
    *,
    now: Optional[float] = None,
    broker_filled_at: Optional[float] = None,
) -> None:
    fs = ensure_fill_sync(state)
    ts = now if now is not None else time.time()
    fs['phase'] = phase
    fs['resolved_at_epoch'] = ts
    fs['next_poll_epoch'] = None
    if phase == 'resolved_estimated':
        fs['audit_due_epoch'] = ts + FILL_ESTIMATE_AUDIT_DELAY_SEC
        fs['audit_attempted'] = False
    from blocks.stop.fill_reference import ensure_fill_reference_epoch

    ensure_fill_reference_epoch(state, broker_filled_at=broker_filled_at)


def should_poll_now(state: Dict[str, Any], *, force: bool = False) -> bool:
    if force:
        return True
    fs = ensure_fill_sync(state)
    if fs.get('phase') in TERMINAL_PHASES:
        return fs.get('phase') not in ('resolved_exact', 'resolved_estimated', 'audit_complete')
    next_epoch = fs.get('next_poll_epoch')
    if next_epoch is None:
        return False
    try:
        return time.time() >= float(next_epoch)
    except (TypeError, ValueError):
        return True


def schedule_next_poll(state: Dict[str, Any], *, delay_sec: Optional[float] = None) -> None:
    fs = ensure_fill_sync(state)
    delay = FILL_SYNC_FAST_SEC if delay_sec is None else delay_sec
    fs['next_poll_epoch'] = time.time() + delay


def log_order_diagnostics(result: OrderResult, *, lot: str = '?') -> None:
    order = result.raw
    if order is None:
        return
    legs = getattr(order, 'legs', None) or []
    parts = []
    for leg in legs:
        action = getattr(leg, 'action', '?')
        fills = getattr(leg, 'fills', None) or []
        rem = getattr(leg, 'remaining_quantity', None)
        parts.append(f'{action}:fills={len(fills)}:rem={rem}')
    if parts:
        log.info('Fill sync diagnostics lot=%s %s', lot, ' '.join(parts))


def apply_audit_correction(state: Dict[str, Any], result: OrderResult) -> bool:
    """Apply broker correction from one-time audit when estimate was wrong."""
    changed = False
    if result.short_fill_price is not None:
        broker_px = round(float(result.short_fill_price), 2)
        cur = float(state_mod.section(state, 'short_leg').get('fill_price') or 0)
        src = state_mod.section(state, 'short_leg').get('fill_price_source')
        if src != SOURCE_BROKER_LEG or abs(cur - broker_px) > FILL_IDENTITY_TOLERANCE:
            state['short_leg']['fill_price'] = broker_px
            state['short_leg']['fill_price_source'] = 'corrected_by_broker'
            changed = True
    if result.long_fill_price is not None:
        broker_px = round(float(result.long_fill_price), 2)
        cur = float(state_mod.section(state, 'long_leg').get('fill_price') or 0)
        src = state_mod.section(state, 'long_leg').get('fill_price_source')
        if src != SOURCE_BROKER_LEG or abs(cur - broker_px) > FILL_IDENTITY_TOLERANCE:
            state['long_leg']['fill_price'] = broker_px
            state['long_leg']['fill_price_source'] = 'corrected_by_broker'
            changed = True
    if changed and try_exact_resolution(state, result):
        entry = state_mod.section(state, 'entry')
        entry['fill_confidence'] = CONFIDENCE_EXACT
        entry['fill_credit_source'] = 'broker_leg_math'
        _append_fill_history(state, {'action': 'audit_correction', 'source': 'broker'})
    return changed


def maybe_run_fill_audit(
    state: Dict[str, Any],
    broker: Any,
    *,
    skip_low_priority: bool = False,
) -> Tuple[bool, Optional[OrderResult]]:
    """One-time delayed audit for resolved_estimated trades."""
    fs = ensure_fill_sync(state)
    if fs.get('phase') != 'resolved_estimated':
        return False, None
    if fs.get('audit_attempted'):
        fs['phase'] = 'audit_complete'
        return False, None
    due = fs.get('audit_due_epoch')
    if due is None or time.time() < float(due):
        return False, None
    if skip_low_priority:
        return False, None

    oid = state.get('open_order_id')
    if not oid:
        return False, None

    fs['audit_attempted'] = True
    result = broker.get_order_status(str(oid))
    if result.success:
        apply_broker_leg_updates(state, result)
        apply_audit_correction(state, result)
    fs['phase'] = 'audit_complete'
    fs['next_poll_epoch'] = None
    return True, result
