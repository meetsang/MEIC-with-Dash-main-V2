"""Append manual spread rows to today's session CSV."""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple

from blocks.session.csv_update import apply_entry_result, try_claim_manual_row
from blocks.session.manual_place import plan_fields_from_request
from blocks.session.plan import SessionPlan, SessionRow, ensure_manual_session
from common.market_hours import is_after_market_close_ct, _parse_expiry_yymmdd
from manual_spread import config as ms_config
from manual_spread.entry import next_lot, parse_expiry
from meic0dte.app.utilities import central_date

log = logging.getLogger(__name__)


def dispatch_manual_place(
    root: Optional[str],
    *,
    launcher_active: bool,
    side: str,
    short_strike: int,
    long_strike: int,
    limit_credit: float,
    quantity: int = ms_config.DEFAULT_QUANTITY,
    expiry: str = '',
    **plan_fields,
) -> Tuple[Dict[str, Any], int]:
    """Append a manual session row and execute entry once.

    When run.py is active, only append to CSV — EntryMonitorRunner places the order.
    When dashboard runs alone, claim the row and run the worker inline.
    """
    if is_after_market_close_ct():
        exp = _parse_expiry_yymmdd(expiry or '')
        # Dashboard manual with no expiry uses today's 0DTE session date.
        if exp is None or exp <= central_date():
            return {
                'status': 'error',
                'error': '0DTE manual entry blocked after 3:00 PM CT market close',
            }, 400

    plan, row = append_manual_session_row(
        root,
        side=side,
        short_strike=short_strike,
        long_strike=long_strike,
        limit_credit=limit_credit,
        quantity=quantity,
        expiry=expiry,
        **plan_fields,
    )

    if launcher_active:
        log.info(
            'Manual place queued for entry monitor: %s (%s)',
            row.slot_key,
            plan.path,
        )
        return {
            'status': 'entering',
            'lot': row.lot,
            'slot_key': row.slot_key,
            'session_path': plan.path,
        }, 200

    if not try_claim_manual_row(plan.path, row.slot_key, strategy=plan.strategy):
        return {
            'status': 'error',
            'error': f'{row.slot_key} already claimed or placed',
        }, 409

    holder: Dict[str, Any] = {'result': None}

    def _work() -> None:
        from blocks.entry.manual_worker import run_manual_entry_row

        fresh = SessionPlan.load(plan.path, strategy=plan.strategy)
        fresh_row = fresh.row_by_slot_key(row.slot_key)
        if fresh_row is None:
            return
        entry_result = run_manual_entry_row(fresh_row)
        apply_entry_result(plan.path, entry_result, strategy=plan.strategy)
        holder['result'] = entry_result.to_manual_api_dict()

    t = threading.Thread(target=_work, name=f'manual-{row.slot_key}', daemon=True)
    t.start()
    t.join(timeout=30)
    result = holder['result'] or {
        'status': 'entering',
        'lot': row.lot,
        'slot_key': row.slot_key,
    }
    code = 200 if result.get('status') in ('placed', 'partial', 'working', 'entering') else 400
    return result, code


def append_manual_session_row(
    root: Optional[str] = None,
    *,
    side: str,
    short_strike: int,
    long_strike: int,
    limit_credit: float,
    quantity: int = ms_config.DEFAULT_QUANTITY,
    expiry: str = '',
    **plan_fields,
) -> Tuple[SessionPlan, SessionRow]:
    """Append a manual Take Trade row (state=entering) to today's MANUAL_SPREAD CSV."""
    normalized = plan_fields_from_request(plan_fields)
    path = ensure_manual_session(root)
    plan = SessionPlan.load(path, strategy=ms_config.STRATEGY)
    lot = next_lot()
    side = side.upper()
    slot_key = f'{lot}_{side}'
    expiry_str = expiry
    if expiry_str:
        try:
            expiry_str = parse_expiry(expiry_str)
        except ValueError:
            pass

    row = SessionRow(
        slot_key=slot_key,
        lot=lot,
        side=side,
        entry_window_start='00:00',
        entry_window_end='23:59',
        entry_condition='manual',
        paused=False,
        skip=False,
        quantity=int(quantity),
        stop_mode='multiplier',
        stop_multiplier=normalized['stop_multiplier'],
        width=normalized['width'],
        credit_min=normalized['credit_min'],
        credit_max=normalized['credit_max'],
        chase1_mode=normalized['chase1_mode'],
        chase1_max=normalized['chase1_max'],
        chase2_mode=normalized['chase2_mode'],
        chase2_max=normalized['chase2_max'],
        fill_wait_sec=normalized['fill_wait_sec'],
        max_attempts=normalized['max_attempts'],
        state='entering',
        trade_path='',
        short_strike=int(short_strike),
        long_strike=int(long_strike),
        limit_credit=float(limit_credit),
        on_unfilled=normalized['on_unfilled'],
        expiry=expiry_str,
    )
    plan.append_row(row)
    plan.save()
    log.info('Appended manual session row %s to %s', slot_key, path)
    return plan, row
