"""Execute one MEIC session row (single side) — scan, place, chase, handoff."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import meic0dte.app.config as meic_config
import meic0dte.app.utilities as util
from blocks.entry.chase import chase_credit_step, chase_kind, max_entry_attempts
from blocks.entry.config import CreditEntryConfig
from blocks.entry.credit_spread import CreditSpreadEntry
from blocks.entry.handoff import apply_stop_snapshot
from blocks.entry.result import EntryWorkerResult
from blocks.session.plan import SessionRow, parse_width
from blocks.stop import state as state_mod
from common.broker_factory import get_broker
from common.integration_report import append_event
from common.streamer_symbols import register_spread_symbols
from meic0dte.open import open_spread_tt
from blocks.entry.spread_scan import SpreadCandidate, pick_meic_candidate, resolve_scan_otm_max

log = logging.getLogger(__name__)

# Re-scan when REST quotes / rate limits yield no in-band pick (e.g. 61/67 symbols).
SCAN_PICK_RETRIES = 3
SCAN_PICK_RETRY_DELAY_SEC = 2.0


@dataclass
class _StrikePick:
    short_symbol: str
    long_symbol: str
    short_strike: int
    long_strike: int
    credit: float


def _entry_config(row: SessionRow) -> CreditEntryConfig:
    wmin, wmax = parse_width(row.width)
    credit_min = float(row.credit_min)
    return CreditEntryConfig(
        spread_width_min=wmin,
        spread_width_max=wmax,
        credit_min=credit_min,
        credit_max_put=float(row.credit_max),
        credit_max_call=float(row.credit_max),
        quantity=int(row.quantity),
        otm_max=resolve_scan_otm_max(credit_min=credit_min),
    )


def _scan_failure_message(lot: str, side: str, candidates: list) -> str:
    if not candidates:
        return (
            f'{lot} {side}: no in-band credit (empty scan — check API quotes or credit band)'
        )
    if candidates[0].overlap_warning:
        return f'{lot} {side}: overlap — {candidates[0].overlap_warning}'
    return f'{lot} {side}: no suitable credit (overlap on all candidates)'


def _scan_pick(entry: CreditSpreadEntry, side: str, expiry: str, lot: str, row_log) -> _StrikePick:
    last_msg = ''
    for scan_attempt in range(1, SCAN_PICK_RETRIES + 1):
        candidates = entry.scan_for_meic(side, expiry, lot)
        pick = pick_meic_candidate(candidates)
        if pick is not None:
            if scan_attempt > 1:
                row_log.info(
                    'Scan pick succeeded on retry %d/%d',
                    scan_attempt,
                    SCAN_PICK_RETRIES,
                )
            return _StrikePick(
                pick.short_symbol,
                pick.long_symbol,
                pick.short_strike,
                pick.long_strike,
                pick.market_credit,
            )
        last_msg = _scan_failure_message(lot, side, candidates)
        if scan_attempt < SCAN_PICK_RETRIES:
            row_log.warning(
                'Scan pick failed (%d/%d): %s — retrying in %.1fs',
                scan_attempt,
                SCAN_PICK_RETRIES,
                last_msg,
                SCAN_PICK_RETRY_DELAY_SEC,
            )
            time.sleep(SCAN_PICK_RETRY_DELAY_SEC)
    raise util.TerminateRequest(last_msg)


def _pick_to_candidate(pick: _StrikePick) -> SpreadCandidate:
    return SpreadCandidate(
        short_symbol=pick.short_symbol,
        long_symbol=pick.long_symbol,
        short_strike=pick.short_strike,
        long_strike=pick.long_strike,
        market_credit=pick.credit,
        short_mid=0.0,
        long_mid=0.0,
    )


def _place_pick(
    broker,
    entry: CreditSpreadEntry,
    pick: _StrikePick,
    side: str,
    quantity: int,
    lot: str,
    row_log,
    *,
    credit: Optional[float] = None,
) -> Tuple[str, str, str, float, int, int]:
    if credit is not None:
        candidate = _pick_to_candidate(_StrikePick(
            pick.short_symbol, pick.long_symbol, pick.short_strike, pick.long_strike, credit,
        ))
        candidate.market_credit = credit
        result = broker.place_spread_order(
            candidate.short_symbol, candidate.long_symbol, quantity, credit,
        )
        if not result.success:
            raise util.TerminateRequest(f'Open order failed: {result.message}')
        return (
            candidate.short_symbol, candidate.long_symbol, result.order_id,
            credit, pick.short_strike, pick.long_strike,
        )
    candidate = _pick_to_candidate(pick)
    return entry.open_candidate(candidate, side, quantity, lot)


def _poll_until_done(
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
        row_log.info('Partial fill %s/%s — polling until full (no chase)', filled, quantity)
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


def _entered_result(row: SessionRow, trade_path: str, order_id: str, filled: int) -> EntryWorkerResult:
    return EntryWorkerResult(
        slot_key=row.slot_key,
        state='entered',
        trade_path=trade_path,
        order_id=str(order_id),
        filled_quantity=filled,
        lot=row.lot,
    )


def _failed_result(row: SessionRow, error: str = '') -> EntryWorkerResult:
    return EntryWorkerResult(
        slot_key=row.slot_key,
        state='failed',
        error=error or 'entry failed',
        lot=row.lot,
    )


def run_meic_entry_row(row: SessionRow, row_log: Optional[logging.Logger] = None) -> EntryWorkerResult:
    """Run entry for one CSV row; returns result for Entry Monitor to persist on CSV."""
    row_log = row_log or logging.getLogger(f'entry.{row.slot_key}')
    broker = get_broker()
    expiry = util.get_expiration_date(row_log)
    entry = CreditSpreadEntry(broker, _entry_config(row), log=row_log)

    quantity = int(row.quantity)
    fill_wait = int(row.fill_wait_sec or meic_config.FILL_WAIT_MAX)
    max_attempts = max_entry_attempts(row)

    entry_ts = state_mod.entry_ts_compact()
    trade_path: Optional[str] = None
    last_pick: Optional[_StrikePick] = None
    last_order_id = ''
    attempt = 0

    try:
        while attempt < max_attempts:
            attempt += 1
            kind = chase_kind(attempt, row)
            if kind == 'exhausted':
                break

            row_log.info('Entry attempt %d/%d mode=%s', attempt, max_attempts, kind)

            if kind in ('initial_scan', 'build_new_strikes'):
                last_pick = _scan_pick(entry, row.side, expiry, row.lot, row_log)
                credit = last_pick.credit
            else:
                if last_pick is None:
                    last_pick = _scan_pick(entry, row.side, expiry, row.lot, row_log)
                credit = chase_credit_step(last_pick.credit)
                if credit < float(row.credit_min):
                    row_log.info('Chase credit %.2f below min %.2f — re-scanning', credit, row.credit_min)
                    last_pick = _scan_pick(entry, row.side, expiry, row.lot, row_log)
                    credit = last_pick.credit

            short_symbol, long_symbol, open_order_id, placed_credit, short_strike, long_strike = _place_pick(
                broker, entry, last_pick, row.side, quantity, row.lot, row_log, credit=credit,
            )
            last_order_id = open_order_id
            last_pick = _StrikePick(short_symbol, long_symbol, short_strike, long_strike, placed_credit)

            reason = 'placed' if trade_path is None else 'cancelled_for_chase'
            trade_path = open_spread_tt.write_pending_trade_state(
                lot=row.lot,
                opt_type=row.side,
                short_symbol=short_symbol,
                long_symbol=long_symbol,
                short_strike=short_strike,
                long_strike=long_strike,
                target_quantity=quantity,
                open_order_id=open_order_id,
                limit_credit=placed_credit,
                existing_path=trade_path,
                entry_ts=entry_ts if attempt == 1 else None,
                reason=reason,
                on_unfilled_step=kind if attempt > 1 else None,
            )
            row_log.info('Handshake %s order=%s', trade_path, open_order_id)

            st = state_mod.load_state(trade_path)
            register_spread_symbols(st, row.lot, row_log)

            state = _poll_until_done(broker, trade_path, quantity, row_log, fill_wait=fill_wait)
            filled = int(state.get('filled_quantity') or 0)
            ostatus = (state.get('open_order') or {}).get('status', 'working')

            append_event({
                'event': 'open_order',
                'lot': row.lot,
                'side': row.side,
                'order_id': open_order_id,
                'status': ostatus.upper() if ostatus != 'partial' else 'PARTIAL',
                'filled_quantity': filled,
                'target_quantity': quantity,
                'short_symbol': short_symbol,
                'long_symbol': long_symbol,
                'credit': placed_credit,
                'short_strike': short_strike,
                'long_strike': long_strike,
                'json_path': trade_path,
            })

            if filled >= quantity or state_mod.section(state, 'open_order').get('fully_filled'):
                apply_stop_snapshot(state, row)
                state_mod.save_state(trade_path, state)
                row_log.info('Full fill %s/%s — handoff to stop monitor', filled, quantity)
                return _entered_result(row, trade_path, open_order_id, filled)

            if filled > 0:
                apply_stop_snapshot(state, row)
                state_mod.save_state(trade_path, state)
                row_log.info('Partial fill %s/%s — handoff (stop waits for full via gate)', filled, quantity)
                return _entered_result(row, trade_path, open_order_id, filled)

            if ostatus in ('cancelled', 'canceled', 'rejected'):
                row_log.info('Order %s %s — retrying', open_order_id, ostatus)
                continue

            row_log.info('No fill after %ss — cancelling %s', fill_wait, open_order_id)
            broker.cancel_order(open_order_id)
            time.sleep(1)

        row_log.error('Entry failed for %s after %d attempts', row.slot_key, attempt)
        return _failed_result(row)
    except util.TerminateRequest as exc:
        row_log.error('Entry terminated: %s', exc)
        return _failed_result(row, str(exc))
    except Exception:
        row_log.exception('Entry worker crashed for %s', row.slot_key)
        return _failed_result(row, 'entry worker failed')
