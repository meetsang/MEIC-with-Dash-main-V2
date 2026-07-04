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
from common.broker_factory import get_broker
from common.streamer_symbols import register_spread_symbols
from manual_spread import config as ms_config
from manual_spread.entry import _resolve_strikes_for_overlap, parse_expiry
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
) -> dict:
    deadline = time.time() + fill_wait
    state = open_spread_tt.sync_trade_state_from_broker(broker, path, row_log)
    while time.time() < deadline:
        filled = int(state.get('filled_quantity') or 0)
        ostatus = (state.get('open_order') or {}).get('status', 'working')
        if filled >= quantity or ostatus == 'filled':
            return state
        if ostatus in ('cancelled', 'canceled', 'rejected'):
            return state
        if filled > 0:
            break
        time.sleep(meic_config.FILL_WAIT)
        state = open_spread_tt.sync_trade_state_from_broker(broker, path, row_log)

    filled = int(state.get('filled_quantity') or 0)
    if 0 < filled < quantity:
        row_log.info('Partial fill %s/%s — polling (no chase)', filled, quantity)
        while True:
            state = open_spread_tt.sync_trade_state_from_broker(broker, path, row_log)
            filled = int(state.get('filled_quantity') or 0)
            ostatus = (state.get('open_order') or {}).get('status', 'working')
            if filled >= quantity or ostatus == 'filled':
                return state
            if ostatus in ('cancelled', 'canceled', 'rejected'):
                return state
            time.sleep(meic_config.FILL_WAIT)
    return state


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


def run_manual_entry_row(
    row: SessionRow,
    row_log: Optional[logging.Logger] = None,
) -> EntryWorkerResult:
    """Place manual spread from session row; CSV update is Entry Monitor's job."""
    row_log = row_log or logging.getLogger(f'entry.{row.slot_key}')

    try:
        broker = get_broker()
        expiry_raw = row.expiry or ''
        expiry_yymmdd = parse_expiry(expiry_raw) if expiry_raw else util.get_expiration_date(row_log)

        short_strike, long_strike, short_symbol, long_symbol, _shifts = _resolve_strikes_for_overlap(
            expiry_yymmdd,
            row.side,
            int(row.short_strike),
            int(row.long_strike),
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

            state = _poll_manual_fill(broker, trade_path, quantity, row_log, fill_wait=fill_wait)
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
