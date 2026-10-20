"""Generic manual close for multi-leg standalone structures."""
from __future__ import annotations

import glob
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from brokers.base import ComboLeg
from common.broker_factory import get_shared_broker_for_strategy
from common.trades_layout import active_dir_for_strategy, history_dir_for_strategy

log = logging.getLogger(__name__)

_CLOSE_ACTION_MAP = {
    'BUY_TO_OPEN': 'SELL_TO_CLOSE',
    'SELL_TO_OPEN': 'BUY_TO_CLOSE',
    'BUY_TO_CLOSE': 'SELL_TO_OPEN',
    'SELL_TO_CLOSE': 'BUY_TO_OPEN',
}


@dataclass
class CloseResult:
    structure_id: str
    success: bool
    message: str = ''
    order_id: Optional[str] = None


def _find_open_trade(strategy_name: str, root: str, trade_id: Optional[str] = None) -> Optional[tuple[str, dict]]:
    today = date.today().isoformat()
    active = active_dir_for_strategy(strategy_name, root)
    if not os.path.isdir(active):
        return None
    candidates = sorted(glob.glob(os.path.join(active, '*.json')), reverse=True)
    for path in candidates:
        base = os.path.basename(path)
        if base.startswith('session_') or base.startswith('halted_'):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                trade = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if trade_id:
            if trade.get('trade_id') == trade_id or trade_id in base:
                return path, trade
        elif trade.get('status') == 'open' or today in base:
            return path, trade
    return None


def _atomic_write(path: str, data: dict) -> None:
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
        f.flush()
    os.replace(tmp, path)


def _reverse_legs(legs: List[dict]) -> List[ComboLeg]:
    out: List[ComboLeg] = []
    for leg in legs:
        action = str(leg.get('action', ''))
        close_action = _CLOSE_ACTION_MAP.get(action)
        if not close_action:
            raise ValueError(f'unsupported leg action for close: {action}')
        out.append(ComboLeg(
            symbol=str(leg['symbol']),
            action=close_action,
            quantity=int(leg.get('qty') or leg.get('quantity') or 1),
        ))
    return out


def close_structure(
    strategy_name: str,
    structure_id: str,
    *,
    root: str,
    paper: Optional[bool] = None,
    trade_id: Optional[str] = None,
) -> CloseResult:
    """Close one structure from today's open trade JSON."""
    found = _find_open_trade(strategy_name, root, trade_id=trade_id)
    if not found:
        return CloseResult(structure_id, False, 'no open trade found')
    path, trade = found

    # Trade provenance safety: an active trade's own execution mode (paper vs live)
    # MUST always govern its close, regardless of what the strategy is set to now.
    trade_paper = trade.get('paper')
    if trade_paper is not None:
        paper = bool(trade_paper)
    elif paper is None:
        from common.broker_factory import is_strategy_paper
        paper = is_strategy_paper(strategy_name, default=True)

    structures = trade.get('structures') or []
    target = next((
        s for s in structures
        if s.get('structure_id') == structure_id or s.get('name') == structure_id or structure_id in ('all', trade.get('trade_id'))
    ), None)
    if target is None and structures:
        target = structures[0]
    if target is None:
        return CloseResult(structure_id, False, f'structure {structure_id!r} not found')
    if target.get('status') in ('closed', 'closing'):
        return CloseResult(structure_id, False, 'already closed')

    log.info('MANUAL_CLOSE_REQUESTED strategy=%s structure=%s trade_id=%s', strategy_name, structure_id, trade_id)
    legs = _reverse_legs(target.get('legs') or [])
    if not legs:
        return CloseResult(structure_id, False, 'no legs to close')
    if len(legs) > 4:
        return CloseResult(structure_id, False, 'too many legs for single order')

    broker = get_shared_broker_for_strategy(strategy_name, paper=paper)
    if paper and getattr(broker, '_is_paper', None) is not True:
        return CloseResult(structure_id, False, 'paper safety check failed')

    is_futures = (
        str(trade.get('structure_type', '')).lower() == 'futures'
        or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES'
        or any(str(leg.symbol).upper() in ('ES', '/ES') for leg in legs)
    )

    if is_futures:
        close_order_id = f'paper-close-{trade.get("trade_id", "fut")}'
        if not paper:
            fut_action = legs[0].action
            res = broker.place_market_order(fut_action, legs[0].symbol, legs[0].quantity)
            if not res.success:
                target['status'] = 'open'
                _atomic_write(path, trade)
                return CloseResult(structure_id, False, res.message or 'futures close order rejected')
            close_order_id = res.order_id
    else:
        mids = broker.fetch_option_mids_api([leg.symbol for leg in legs])
        net = 0.0
        for leg in legs:
            mid = mids.get(leg.symbol, 0.5)
            if leg.action.startswith('BUY'):
                net -= mid * leg.quantity
            else:
                net += mid * leg.quantity
        # Closing a debit structure: expect credit (positive net)
        price = net if net >= 0 else net

        target['status'] = 'closing'
        _atomic_write(path, trade)

        result = broker.place_multi_leg_order(legs, price=price)
        if not result.success:
            target['status'] = 'open'
            _atomic_write(path, trade)
            return CloseResult(structure_id, False, result.message or 'order rejected')
        close_order_id = result.order_id

    now_iso = datetime.now().isoformat()
    target['status'] = 'closed'
    target['closed_manually'] = True
    target['closed_at'] = now_iso
    target['exit_fill_time'] = now_iso
    target['close_order_id'] = close_order_id
    target['realized_pnl'] = target.get('unrealized_pnl', 0.0)

    open_count = sum(1 for s in structures if s.get('status') not in ('closed', 'cancelled'))
    if open_count == 0:
        trade['status'] = 'closed'
        trade['close_mechanism'] = 'manual_close'
        trade['closed_at'] = now_iso
        trade['exited_at'] = now_iso
        trade['realized_pnl'] = trade.get('unrealized_pnl', 0.0)
        trade['unrealized_pnl'] = 0.0
        trade['unrealized_pnl_dollars'] = 0.0
        multiplier = float(trade.get('multiplier') or (50.0 if is_futures else 100.0))
        if 'realized_pnl_dollars' not in trade or trade['realized_pnl_dollars'] == 0.0:
            trade['realized_pnl_dollars'] = round(float(trade['realized_pnl']) * int(trade.get('quantity') or 1) * multiplier, 2)
        # Move trade to history directory
        hist_dir = history_dir_for_strategy(strategy_name, root)
        os.makedirs(hist_dir, exist_ok=True)
        hist_path = os.path.join(hist_dir, os.path.basename(path))
        with open(hist_path, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)
        try:
            os.remove(path)
        except OSError:
            pass
    else:
        _atomic_write(path, trade)

    log.info('MANUAL_CLOSE_FILLED strategy=%s structure=%s order=%s', strategy_name, structure_id, result.order_id)
    return CloseResult(structure_id, True, 'filled', order_id=result.order_id)


def close_all_structures(strategy_name: str, *, root: str, paper: bool = True) -> List[CloseResult]:
    active = active_dir_for_strategy(strategy_name, root)
    if not os.path.isdir(active):
        return []
    candidates = sorted(glob.glob(os.path.join(active, '*.json')), reverse=True)
    results: List[CloseResult] = []
    for path in candidates:
        base = os.path.basename(path)
        if base.startswith('session_') or base.startswith('halted_'):
            continue
        try:
            with open(path, encoding='utf-8') as f:
                trade = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if trade.get('status') != 'open':
            continue
        tid = trade.get('trade_id') or base
        results.append(close_structure(strategy_name, 'all', root=root, paper=paper, trade_id=tid))
    return results
