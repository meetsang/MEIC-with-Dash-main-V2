"""V3 exit field backfill, recovery, and stall reconciliation (§8.5, §6.7)."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from brokers.base import BrokerBase
from blocks.stop import state as state_mod
from blocks.stop.v3 import config as v3_config
from blocks.stop.v3.trade_slot import TradeSlot, save_slot

log = logging.getLogger(__name__)


def ensure_v3_exit_fields(state: Dict[str, Any], *, mechanism: Optional[str] = None) -> None:
    """Backfill V3 fields from V2 state on first load."""
    if state.get('close_only_mode') is None and mechanism:
        if mechanism in ('manual_close', 'admin_killswitch'):
            state['close_only_mode'] = True
    elif state.get('close_only_mode') is None:
        mech = str(state.get('close_mechanism') or '').lower()
        status = str(state.get('status') or '')
        if mech in ('manual_close', 'admin_killswitch') and status in ('closing', 'open'):
            state['close_only_mode'] = True
        elif state.get('spread_close_order_id') and mech in ('manual_close', 'admin_killswitch'):
            state['close_only_mode'] = True
        else:
            state.setdefault('close_only_mode', False)

    if not state.get('exit_handler') and mechanism:
        state['exit_handler'] = mechanism
    elif not state.get('exit_handler'):
        mech = str(state.get('close_mechanism') or '').lower()
        if mech:
            state['exit_handler'] = mech


def mark_exit_started(
    state: Dict[str, Any],
    *,
    step: str,
    mechanism: str,
) -> None:
    state['close_only_mode'] = True
    state['exit_handler'] = mechanism
    state['exit_started_at'] = state_mod.now_iso()
    state['exit_last_step'] = step
    state['exit_last_progress_at'] = state_mod.now_iso()
    state['exit_attempt'] = int(state.get('exit_attempt') or 0) + 1
    state.pop('exit_error', None)
    state.pop('exit_stalled', None)


def mark_exit_progress(state: Dict[str, Any], step: str) -> None:
    state['exit_last_step'] = step
    state['exit_last_progress_at'] = state_mod.now_iso()
    state.pop('exit_stalled', None)


def mark_exit_error(state: Dict[str, Any], error: str, *, step: str) -> None:
    state['exit_error'] = error
    state['exit_last_step'] = step
    state['exit_last_progress_at'] = state_mod.now_iso()


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def check_exit_stall(slot: TradeSlot) -> bool:
    """Set exit_stalled when no progress for STOP_EXIT_STALL_SEC (§6.3.2)."""
    st = slot.state
    if st.get('status') in ('closed', 'cancelled'):
        return False
    if not st.get('exit_handler') and not st.get('close_only_mode'):
        return False
    last = _parse_iso(str(st.get('exit_last_progress_at') or ''))
    if last is None:
        return False
    age = (datetime.now().astimezone() - last).total_seconds()
    if age >= v3_config.STOP_EXIT_STALL_SEC:
        if not st.get('exit_stalled'):
            st['exit_stalled'] = True
            return True
    return bool(st.get('exit_stalled'))


def recover_route(slot: TradeSlot) -> Optional[str]:
    """Return recovery route label for startup (§6.7), or None."""
    st = slot.state
    status = str(st.get('status') or '')
    if status in ('closed', 'cancelled'):
        return None
    if st.get('spread_close_order_id'):
        return 'resume_spread_close_poll'
    if st.get('short_closed_at') and status == 'closing':
        return 'resume_long_chase'
    if st.get('close_only_mode') or st.get('exit_handler'):
        return 'resume_exit_handler'
    return None


def reconcile_stalled_exit(slot: TradeSlot, broker: BrokerBase) -> Tuple[bool, str]:
    """
    Broker reconcile for stalled exit (§6.3.2).
    Updates state from broker; clears exit_stalled when forward progress is proven.
    Does NOT spawn a replacement worker — operator alert path only.
    Returns (progress_made, summary).
    """
    st = slot.state
    if not st.get('exit_stalled'):
        return False, 'not_stalled'

    progress = False
    notes: list[str] = []

    sc_oid = st.get('spread_close_order_id')
    if sc_oid:
        result = broker.get_order_status(str(sc_oid))
        if result.success:
            status = str(result.status).lower()
            notes.append(f'spread_close={status}')
            if status == 'filled':
                if result.short_fill_price is not None:
                    st['short_close_price'] = result.short_fill_price
                if result.long_fill_price is not None:
                    st['long_close_price'] = result.long_fill_price
                st['spread_close_order_id'] = None
                st['status'] = 'closed'
                progress = True
            elif status in ('cancelled', 'canceled', 'rejected', 'expired'):
                st['spread_close_order_id'] = None
                progress = True

    lc_oid = st.get('long_close_order_id')
    if lc_oid and st.get('status') == 'closing':
        result = broker.get_order_status(str(lc_oid))
        if result.success:
            status = str(result.status).lower()
            notes.append(f'long_close={status}')
            if status == 'filled':
                if result.filled_price is not None:
                    st['long_close_price'] = float(result.filled_price)
                st['status'] = 'closed'
                progress = True

    active = st.get('active_stop') or {}
    stop_oid = active.get('order_id')
    if stop_oid and st.get('status') == 'open':
        result = broker.get_order_status(str(stop_oid))
        if result.success:
            notes.append(f'stop={result.status}')
            active['status'] = result.status

    if progress:
        st.pop('exit_stalled', None)
        st.pop('exit_error', None)
        mark_exit_progress(st, 'stall_reconcile_progress')
        save_slot(slot)
        log.info('Stall reconcile progress on %s: %s', slot.path, ', '.join(notes))
    else:
        mark_exit_progress(st, 'stall_reconcile_checked')
        save_slot(slot)
        log.critical(
            'Exit still stalled on %s after broker reconcile (%s) — operator review',
            slot.path,
            ', '.join(notes) or 'no_working_orders_found',
        )

    return progress, ', '.join(notes) or 'checked'
