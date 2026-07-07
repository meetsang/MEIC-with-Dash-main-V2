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
    route = resolve_exit_recovery_route(slot)
    if route == 'none':
        return None
    return route


def resolve_exit_recovery_route(slot: TradeSlot) -> str:
    """
    Explicit F-4 recovery routing. Never route breach_* to manual kill.
    """
    st = slot.state
    status = str(st.get('status') or '')
    if status in ('closed', 'cancelled'):
        return 'none'

    if st.get('spread_close_order_id'):
        return 'poll_close_order'

    exit_handler = str(st.get('exit_handler') or '')
    close_mech = str(st.get('close_mechanism') or '')

    if exit_handler in ('manual_close', 'admin_killswitch'):
        if st.get('spread_close_order_id'):
            return 'poll_close_order'
        if status == 'closing':
            return 'poll_close_order'
        return 'resume_manual_kill'

    if exit_handler == 'exchange_stop' or close_mech == 'exchange_stop':
        if st.get('short_closed_at') and status == 'closing':
            return 'resume_long_chase'
        if st.get('spread_close_order_id'):
            return 'poll_close_order'
        return 'none'

    if exit_handler.startswith('breach_') or close_mech in (
        'software_breach', 'spread_stop_breach', 'breach_limit_reprice',
    ):
        if st.get('spread_close_order_id'):
            return 'poll_close_order'
        if st.get('short_closed_at') and status == 'closing':
            return 'resume_long_chase'
        active = st.get('active_stop') or {}
        if active.get('type') == 'LIMIT' and active.get('order_id'):
            return 'resume_breach_exit'
        return 'none'

    if exit_handler == 'phase3_spx_proximity' or close_mech == 'phase3_proximity':
        if st.get('spread_close_order_id'):
            return 'poll_close_order'
        if st.get('short_closed_at') and status == 'closing':
            return 'resume_long_chase'
        return 'resume_phase3_exit'

    if exit_handler == 'phase2_net_credit_upgrade':
        log.error('Invalid exit_handler phase2 on %s — quarantine', slot.path)
        return 'quarantine'

    if st.get('close_only_mode') and not exit_handler:
        return 'quarantine'

    return 'none'


def exit_action_confirmed(state: Dict[str, Any]) -> bool:
    """True when phase/handler work initiated a real exit (F-3)."""
    status = str(state.get('status') or '')
    if status in ('closing', 'closed'):
        return True
    if state.get('spread_close_order_id'):
        return True
    if state.get('short_closed_at'):
        return True
    mech = str(state.get('close_mechanism') or '').lower()
    if mech in ('phase3_proximity',):
        return state.get('short_closed_at') is not None
    if mech in ('software_breach', 'spread_stop_breach', 'breach_limit_reprice'):
        phases = state.get('phases') or {}
        if phases.get('breach_limit_placed_at'):
            return True
    if str(state.get('exit_handler') or '').startswith('breach_'):
        phases = state.get('phases') or {}
        if phases.get('breach_limit_placed_at'):
            return True
    return False


def finalize_v3_exit_state(state: Dict[str, Any]) -> None:
    """Clear active recovery triggers after close; preserve audit (F-6)."""
    audit = dict(state.get('exit_audit') or {})
    if state.get('exit_handler'):
        audit.setdefault('handler', state.get('exit_handler'))
    if state.get('exit_started_at'):
        audit.setdefault('started_at', state.get('exit_started_at'))
    audit['finished_at'] = state_mod.now_iso()
    if state.get('exit_last_step'):
        audit['last_step'] = state.get('exit_last_step')
    if state.get('spread_close_order_id'):
        audit['spread_close_order_id'] = state.get('spread_close_order_id')
    state['exit_audit'] = audit
    state['close_only_mode'] = False
    state['exit_handler'] = None
    state['exit_started_at'] = None
    state['exit_last_step'] = 'finalized_closed'
    state.pop('exit_stalled', None)
    state.pop('exit_error', None)


def fill_timestamp_from_broker_result(result) -> Optional[float]:
    """R-9 — use broker fill time when available."""
    if result is None:
        return None
    if getattr(result, 'filled_at', None) is not None:
        return float(result.filled_at)
    raw = getattr(result, 'raw', None)
    if raw is None:
        return None
    try:
        latest = None
        for leg in getattr(raw, 'legs', None) or []:
            for fill in getattr(leg, 'fills', None) or []:
                ts = getattr(fill, 'filled_at', None) or getattr(fill, 'fill_time', None)
                if ts is None:
                    continue
                if hasattr(ts, 'timestamp'):
                    val = float(ts.timestamp())
                else:
                    val = float(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp())
                if latest is None or val > latest:
                    latest = val
        return latest
    except (TypeError, ValueError, AttributeError):
        return None


def spread_close_preflight_blocked(
    broker: BrokerBase,
    state: Dict[str, Any],
    *,
    short_sym: str,
    long_sym: str,
    qty: int,
) -> Optional[str]:
    """
    Returns block reason if spread close must not transmit (F-5/F-9).
    None means OK to proceed.
    """
    status = str(state.get('status') or '')
    if status in ('closed', 'cancelled'):
        return 'already_terminal'
    if state.get('exit_last_step') in ('spread_close_filled', 'finalized_closed'):
        return 'already_finalized'
    if state.get('spread_close_order_id'):
        return 'existing_close_order'

    position_state = broker.inspect_spread_position(
        short_sym, long_sym, expected_qty=qty,
    )
    if position_state in ('flat', 'not_closable', 'mismatch', 'unknown'):
        return position_state
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
