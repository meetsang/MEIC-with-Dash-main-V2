"""One active opening order per slot — auditable attempt chain and replacement guard."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from blocks.stop import state as state_mod

ENTRY_DUPLICATE_RISK_BLOCKED = 'ENTRY_DUPLICATE_RISK_BLOCKED'

_TERMINAL_STATUSES = frozenset({'cancelled', 'canceled', 'rejected', 'filled', 'expired'})
_BLOCKING_STATUSES = frozenset({'working', 'partial', 'partially filled', 'visibility_unknown'})


def entry_attempts(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = state.get('entry_attempts')
    return list(raw) if isinstance(raw, list) else []


def record_entry_attempt(
    state: Dict[str, Any],
    *,
    attempt: int,
    order_id: str,
    status: str = 'working',
    terminal_confirmed: bool = False,
    filled_quantity: int = 0,
) -> None:
    chain = entry_attempts(state)
    chain.append({
        'attempt': attempt,
        'order_id': str(order_id),
        'placed_at_epoch': time.time(),
        'status': status,
        'terminal_confirmed': terminal_confirmed,
        'filled_quantity': filled_quantity,
    })
    state['entry_attempts'] = chain
    state['current_open_order_id'] = str(order_id)


def update_latest_attempt_status(
    state: Dict[str, Any],
    *,
    status: str,
    terminal_confirmed: bool = False,
    filled_quantity: Optional[int] = None,
) -> None:
    chain = entry_attempts(state)
    if not chain:
        return
    latest = dict(chain[-1])
    latest['status'] = status
    latest['terminal_confirmed'] = terminal_confirmed
    if filled_quantity is not None:
        latest['filled_quantity'] = filled_quantity
    chain[-1] = latest
    state['entry_attempts'] = chain


def _latest_attempt_blocks_replacement(state: Dict[str, Any]) -> Optional[str]:
    chain = entry_attempts(state)
    if not chain:
        return None
    latest = chain[-1]
    status = str(latest.get('status') or '').lower()
    if status == 'visibility_unknown':
        return 'previous_visibility_unknown'
    if int(latest.get('filled_quantity') or 0) > 0:
        return 'previous_partial_or_filled'
    if status in _BLOCKING_STATUSES:
        return f'previous_order_{status}'
    if status in _TERMINAL_STATUSES:
        if latest.get('terminal_confirmed') and int(latest.get('filled_quantity') or 0) == 0:
            return None
        return 'previous_cancel_unconfirmed'
    return None


def _broker_blocks_replacement(
    broker,
    *,
    short_symbol: str,
    long_symbol: str,
    quantity: int,
) -> Optional[str]:
    inspect = getattr(broker, 'inspect_spread_position', None)
    if inspect is not None:
        pos = inspect(short_symbol, long_symbol, expected_qty=quantity)
        if pos == 'closable':
            return 'broker_position_already_open'
    find_working = getattr(broker, 'find_working_open_spread_orders', None)
    if find_working is not None:
        working = find_working(short_symbol, long_symbol)
        if working:
            return 'broker_working_opening_order'
    return None


def assert_replacement_allowed(
    broker,
    state: Dict[str, Any],
    *,
    short_symbol: str,
    long_symbol: str,
    quantity: int,
    is_initial_place: bool = False,
) -> Tuple[bool, str]:
    """
    Return (allowed, reason). Blocks replacement when prior attempt is not
    terminal-confirmed unfilled and broker shows working order or open position.
    """
    if is_initial_place and not entry_attempts(state):
        return True, ''

    reason = _latest_attempt_blocks_replacement(state)
    if reason:
        return False, reason

    oo = state_mod.section(state, 'open_order')
    ostatus = str(oo.get('status') or '').lower()
    if state.get('entry_control') == 'cooldown_blind' or ostatus == 'visibility_unknown':
        return False, 'cooldown_blind_active'

    if int(state.get('filled_quantity') or 0) > 0:
        return False, 'trade_already_has_fill'

    broker_reason = _broker_blocks_replacement(
        broker,
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        quantity=quantity,
    )
    if broker_reason:
        return False, broker_reason

    return True, ''
