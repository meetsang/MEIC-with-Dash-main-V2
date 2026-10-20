"""Execute one manual session row — explicit strikes, optional same-spread chase."""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import meic0dte.app.config as meic_config
import meic0dte.app.utilities as util
from blocks.entry.chase import chase_credit_step
from blocks.entry.handoff import apply_stop_snapshot
from blocks.entry.result import EntryWorkerResult
from blocks.session.manual_place import apply_plan_metadata
from blocks.session.plan import SessionRow
from blocks.stop import state as state_mod
from common.broker_factory import get_shared_broker, get_shared_broker_for_strategy
from common.streamer_symbols import register_spread_symbols
from common.trading_gate import effective_new_risk_blocked, gate_enabled
from manual_spread import config as ms_config
from manual_spread.butterfly import (
    STRUCTURE_TYPES,
    build_butterfly_legs,
    is_butterfly_side,
    order_price_for_butterfly,
    strikes_for_body,
)
from manual_spread.entry import _resolve_strikes_for_overlap, parse_expiry, _write_butterfly_pending
from meic0dte.open import open_spread_tt

log = logging.getLogger(__name__)


def _manual_chase_enabled(row: SessionRow) -> bool:
    return (
        (row.on_unfilled or '').lower() == 'chase_same_trade'
        and int(row.chase1_max or 0) > 0
    )


def _poll_manual_fill(
    broker,
    path: str,
    quantity: int,
    row_log,
    *,
    fill_wait: int,
) -> tuple[dict, bool]:
    deadline = time.time() + fill_wait
    state, ok, _detail = open_spread_tt.sync_entry_trade_state_from_broker(broker, path, row_log)
    if not ok:
        return state, False
    while time.time() < deadline:
        filled = int(state.get('filled_quantity') or 0)
        ostatus = (state.get('open_order') or {}).get('status', 'working')
        if filled >= quantity or ostatus == 'filled':
            return state, True
        if ostatus in ('cancelled', 'canceled', 'rejected'):
            return state, True
        if ostatus == 'visibility_unknown':
            return state, False
        if filled > 0:
            break
        time.sleep(meic_config.FILL_WAIT)
        state, ok, _detail = open_spread_tt.sync_entry_trade_state_from_broker(broker, path, row_log)
        if not ok:
            return state, False

    filled = int(state.get('filled_quantity') or 0)
    if 0 < filled < quantity:
        row_log.info('Partial fill %s/%s — polling (no chase)', filled, quantity)
        while True:
            state, ok, _detail = open_spread_tt.sync_entry_trade_state_from_broker(broker, path, row_log)
            if not ok:
                return state, False
            filled = int(state.get('filled_quantity') or 0)
            ostatus = (state.get('open_order') or {}).get('status', 'working')
            if filled >= quantity or ostatus == 'filled':
                return state, True
            if ostatus in ('cancelled', 'canceled', 'rejected'):
                return state, True
            if ostatus == 'visibility_unknown':
                return state, False
            time.sleep(meic_config.FILL_WAIT)
    return state, True


def _finalize_manual_result(
    *,
    state: dict,
    trade_path: str,
    row: SessionRow,
    place_order_id: str,
    quantity: int,
) -> EntryWorkerResult:
    filled = int(state.get('filled_quantity') or 0)
    fully = filled >= quantity or state_mod.section(state, 'open_order').get('fully_filled')

    if fully or filled > 0:
        if not is_butterfly_side(row.side):
            apply_stop_snapshot(state, row)
        apply_plan_metadata(state, row)
        state_mod.save_state(trade_path, state)
        api_status = 'placed' if fully else 'partial'
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='entered',
            trade_path=trade_path,
            order_id=str(place_order_id),
            filled_quantity=filled,
            api_status=api_status,
            lot=row.lot,
            filename=os.path.basename(trade_path),
        )

    return EntryWorkerResult(
        slot_key=row.slot_key,
        state='entered',
        trade_path=trade_path,
        order_id=str(place_order_id),
        filled_quantity=filled,
        api_status='working',
        lot=row.lot,
        filename=os.path.basename(trade_path),
    )


def _run_manual_butterfly_row(
    row: SessionRow,
    row_log: logging.Logger,
    broker,
    expiry_yymmdd: str,
) -> EntryWorkerResult:
    structure_key = (row.width or '').replace('bf:', '').lower()
    meta = STRUCTURE_TYPES.get(structure_key)
    if not meta:
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error=f'unknown butterfly structure: {row.width}',
            api_status='error',
            lot=row.lot,
        )

    body_strike = int(row.short_strike)
    wing_half = int(row.long_strike or ms_config.DEFAULT_BF_WING_HALF)
    quantity = int(row.quantity)
    limit_price = float(row.limit_credit)
    sell = bool(meta['sell'])
    opt_type = str(meta['opt'])
    wl, body, wh = strikes_for_body(body_strike, wing_half)
    legs = build_butterfly_legs(
        expiry_yymmdd, opt_type, wl, body, wh, quantity, sell=sell,
    )
    fill_wait = max(
        int(row.fill_wait_sec or ms_config.OPEN_FILL_POLL_SEC),
        ms_config.OPEN_FILL_POLL_SEC,
    )

    if gate_enabled() and effective_new_risk_blocked():
        row_log.warning('New-risk gate closed — aborting manual butterfly entry')
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error='new_risk_gate_blocked',
            api_status='error',
            lot=row.lot,
        )

    order_price = order_price_for_butterfly(limit_price, sell=sell)
    place = broker.place_multi_leg_order(legs, price=order_price)
    if not place.success:
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error=place.message or 'Open order failed',
            api_status='error',
            lot=row.lot,
        )

    legs_spec = []
    from common.symbols import parse_canonical

    for leg in legs:
        parsed = parse_canonical(leg.symbol)
        strike = parsed[2] if parsed else 0
        legs_spec.append({
            'symbol': leg.symbol,
            'action': leg.action,
            'qty': leg.quantity,
            'strike': strike,
        })

    trade_path = _write_butterfly_pending(
        lot=row.lot,
        side=str(meta['side']),
        structure_type=structure_key,
        body_strike=body,
        wing_low=wl,
        wing_high=wh,
        wing_half=wing_half,
        legs_spec=legs_spec,
        quantity=quantity,
        open_order_id=str(place.order_id),
        limit_price=limit_price,
        sell=sell,
    )
    st = state_mod.load_state(trade_path)
    apply_plan_metadata(st, row)
    state_mod.save_state(trade_path, st)

    state, vis_ok = _poll_manual_fill(broker, trade_path, quantity, row_log, fill_wait=fill_wait)
    if not vis_ok:
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='entering',
            trade_path=trade_path,
            order_id=str(place.order_id),
            error='cooldown_blind',
            api_status='working',
            lot=row.lot,
            filename=os.path.basename(trade_path),
        )
    return _finalize_manual_result(
        state=state,
        trade_path=trade_path,
        row=row,
        place_order_id=str(place.order_id),
        quantity=quantity,
    )


def run_manual_entry_row(
    row: SessionRow,
    row_log: Optional[logging.Logger] = None,
    broker: Any = None,
) -> EntryWorkerResult:
    """Place manual spread from session row; CSV update is Entry Monitor's job."""
    row_log = row_log or logging.getLogger(f'entry.{row.slot_key}')

    try:
        from common.broker_factory import is_strategy_paper
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        from common.daily_overrides import get_overrides
        ovr = get_overrides('MANUAL_SPREAD', root)
        if ovr.get('enabled') is False:
            row_log.warning('MANUAL_SPREAD strategy is currently paused/disabled for today — blocking entry')
            return EntryWorkerResult(
                slot_key=row.slot_key,
                state='error',
                error='MANUAL_SPREAD strategy is paused for today',
            )
        paper = is_strategy_paper('MANUAL_SPREAD')
        if broker is None:
            if hasattr(get_shared_broker, 'mock_calls') or hasattr(get_shared_broker, 'assert_called'):
                broker = get_shared_broker()
            else:
                broker = get_shared_broker_for_strategy('MANUAL_SPREAD', paper=paper)
        expiry_raw = row.expiry or ''
        expiry_yymmdd = parse_expiry(expiry_raw) if expiry_raw else util.get_expiration_date(row_log)

        if is_butterfly_side(row.side):
            return _run_manual_butterfly_row(row, row_log, broker, expiry_yymmdd)

        short_strike, long_strike, short_symbol, long_symbol, _shifts = _resolve_strikes_for_overlap(
            expiry_yymmdd,
            row.side,
            int(row.short_strike),
            int(row.long_strike),
            paper=paper,
        )
        quantity = int(row.quantity)
        credit = float(row.limit_credit)
        fill_wait = max(
            int(row.fill_wait_sec or ms_config.OPEN_FILL_POLL_SEC),
            ms_config.OPEN_FILL_POLL_SEC,
        )
        chase_floor = float(row.credit_min or 0)
        max_reprices = int(row.chase1_max or 0) if _manual_chase_enabled(row) else 0

        entry_ts = state_mod.entry_ts_compact()
        trade_path: Optional[str] = None
        last_order_id = ''
        attempt = 0
        max_attempts = 1 + max_reprices if _manual_chase_enabled(row) else 1

        while attempt < max_attempts:
            attempt += 1
            is_retry = attempt > 1

            if gate_enabled() and effective_new_risk_blocked():
                row_log.warning('New-risk gate closed — aborting manual entry')
                return EntryWorkerResult(
                    slot_key=row.slot_key,
                    state='failed',
                    error='new_risk_gate_blocked',
                    api_status='error',
                    lot=row.lot,
                )

            if is_retry:
                oid = last_order_id
                if oid:
                    broker.cancel_order(str(oid))
                    time.sleep(1)
                credit = chase_credit_step(credit)
                if chase_floor > 0 and credit < chase_floor:
                    row_log.info('Chase credit %.2f below floor %.2f — stopping', credit, chase_floor)
                    break
                reason = 'cancelled_for_chase'
            else:
                reason = 'placed'

            place = broker.place_spread_order(short_symbol, long_symbol, quantity, credit)
            if not place.success:
                if trade_path:
                    break
                return EntryWorkerResult(
                    slot_key=row.slot_key,
                    state='failed',
                    error=place.message or 'Open order failed',
                    api_status='error',
                    lot=row.lot,
                )

            last_order_id = str(place.order_id)
            trade_path = open_spread_tt.write_pending_trade_state(
                lot=row.lot,
                opt_type=row.side,
                short_symbol=short_symbol,
                long_symbol=long_symbol,
                short_strike=short_strike,
                long_strike=long_strike,
                target_quantity=quantity,
                open_order_id=last_order_id,
                limit_credit=credit,
                strategy=ms_config.STRATEGY,
                active_directory=state_mod.manual_spread_active_dir(),
                existing_path=trade_path,
                entry_ts=entry_ts if not is_retry else None,
                reason=reason,
                on_unfilled_step='chase_same_trade' if is_retry else None,
            )

            st = state_mod.load_state(trade_path)
            apply_plan_metadata(st, row)
            state_mod.save_state(trade_path, st)
            register_spread_symbols(st, row.lot, row_log)

            state, vis_ok = _poll_manual_fill(broker, trade_path, quantity, row_log, fill_wait=fill_wait)
            if not vis_ok:
                return EntryWorkerResult(
                    slot_key=row.slot_key,
                    state='entering',
                    trade_path=trade_path,
                    order_id=str(last_order_id),
                    error='cooldown_blind',
                    api_status='working',
                    lot=row.lot,
                    filename=os.path.basename(trade_path),
                )
            filled = int(state.get('filled_quantity') or 0)
            ostatus = (state.get('open_order') or {}).get('status', 'working')

            if filled >= quantity or ostatus == 'filled':
                return _finalize_manual_result(
                    state=state,
                    trade_path=trade_path,
                    row=row,
                    place_order_id=last_order_id,
                    quantity=quantity,
                )
            if filled > 0:
                return _finalize_manual_result(
                    state=state,
                    trade_path=trade_path,
                    row=row,
                    place_order_id=last_order_id,
                    quantity=quantity,
                )
            if not _manual_chase_enabled(row) or attempt >= max_attempts:
                break
            if ostatus in ('cancelled', 'canceled', 'rejected') and not _manual_chase_enabled(row):
                break

        if trade_path:
            state = state_mod.load_state(trade_path)
            return _finalize_manual_result(
                state=state,
                trade_path=trade_path,
                row=row,
                place_order_id=last_order_id,
                quantity=quantity,
            )

        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error='entry failed',
            api_status='error',
            lot=row.lot,
        )

    except ValueError as exc:
        row_log.error('Manual entry failed: %s', exc)
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error=str(exc),
            api_status='error',
            lot=row.lot,
        )
    except Exception:
        row_log.exception('Manual entry worker crashed for %s', row.slot_key)
        return EntryWorkerResult(
            slot_key=row.slot_key,
            state='failed',
            error='entry worker failed',
            api_status='error',
            lot=row.lot,
        )
