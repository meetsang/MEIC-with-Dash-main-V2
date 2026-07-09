"""Expiry cutoff: settle closed trades or freeze broker actions."""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Literal, Optional, Tuple

from blocks.stop import state as state_mod
from common.expiry_settlement import (
    compute_settled_pnl,
    ensure_spx_settlement_close,
    get_spx_settlement_close,
    settlement_cutoff_reached,
)
from common.session_cleanup import trade_expiry_date

Outcome = Literal['ok', 'settled', 'frozen', 'already_closed']


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def _clear_broker_order_fields(state: Dict[str, Any]) -> None:
    state['active_stop'] = None
    state['stop_quantity'] = 0
    state.pop('spread_close_order_id', None)
    state.pop('long_close_order_id', None)


def _apply_frozen_pending(state: Dict[str, Any]) -> None:
    _clear_broker_order_fields(state)
    state['broker_actions_frozen'] = True
    state['expiry_settlement_pending'] = True
    state['broker_actions_disabled_reason'] = 'expired_option'
    state.pop('broker_actions_paused', None)
    state.pop('broker_actions_pause_reason', None)
    state.pop('stop_rearm_pending', None)


def _apply_settlement(state: Dict[str, Any], settled: Dict[str, Any], spx: float) -> None:
    _clear_broker_order_fields(state)
    state['status'] = 'closed'
    state['close_mechanism'] = 'expiry_settlement'
    state['settled_at_expiry'] = True
    state['short_close_price'] = settled['short_close_price']
    state['long_close_price'] = settled['long_close_price']
    state['close_debit'] = settled['close_debit']
    state['pnl'] = settled['pnl']
    state['spx_close'] = settled.get('spx_close', spx)
    state.pop('broker_actions_frozen', None)
    state.pop('expiry_settlement_pending', None)
    state.pop('broker_actions_disabled', None)
    state.pop('broker_actions_disabled_reason', None)
    state.pop('broker_actions_paused', None)
    state.pop('broker_actions_pause_reason', None)
    state.pop('stop_rearm_pending', None)
    state_mod.append_stop_history(
        state,
        action='settled',
        order_id=None,
        price=None,
        phase=0,
        reason='expiry_settlement',
        spx_price_at_event=spx,
    )


def try_settle_or_freeze_trade(
    state: Dict[str, Any],
    *,
    path: str = '',
    root: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[Outcome, Dict[str, Any]]:
    """Settle at expiry when SPX is available; otherwise freeze without closing."""
    status = (state.get('status') or '').lower()
    if status == 'closed':
        return 'already_closed', state
    if status not in ('open', 'closing'):
        return 'ok', state

    filename = os.path.basename(path) if path else ''
    expiry = trade_expiry_date(state, filename)
    if expiry is None or not settlement_cutoff_reached(expiry, now=now):
        return 'ok', state

    root = root or _project_root()
    spx = ensure_spx_settlement_close(expiry, root=root, now=now)
    if spx is None:
        spx = get_spx_settlement_close(expiry, root=root, now=now)
    if spx is None:
        _apply_frozen_pending(state)
        return 'frozen', state

    settled = compute_settled_pnl(state, spx, now=now)
    if settled is None:
        _apply_frozen_pending(state)
        return 'frozen', state

    _apply_settlement(state, settled, spx)
    return 'settled', state
