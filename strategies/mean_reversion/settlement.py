"""Mean Reversion EOD settlement and PnL calculation helpers."""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Any, Dict, Optional

from common.expiry_settlement import resolve_daily_close
from common.symbols import parse_canonical
from common.trades_layout import history_dir_for_strategy

SPX_OPTION_MULTIPLIER = 100


def pnl_points_to_dollars(pnl_points: float, quantity: int = 1) -> float:
    """Convert SPX option premium-point P/L to dollars (×100 per contract)."""
    return round(float(pnl_points) * SPX_OPTION_MULTIPLIER * int(quantity or 1), 2)


def attach_dollar_pnl_display(trade: dict) -> None:
    """Add *_dollars fields alongside premium-point P/L on a Mean Reversion trade dict."""
    qty = int(trade.get('quantity') or 1)
    if trade.get('unrealized_pnl') is not None:
        trade['unrealized_pnl_dollars'] = pnl_points_to_dollars(trade['unrealized_pnl'], qty)
    if trade.get('realized_pnl') is not None:
        trade['realized_pnl_dollars'] = pnl_points_to_dollars(trade['realized_pnl'], qty)
    for struct in trade.get('structures') or []:
        if struct.get('unrealized_pnl') is not None:
            struct['unrealized_pnl_dollars'] = pnl_points_to_dollars(struct['unrealized_pnl'], qty)
        if struct.get('realized_pnl') is not None:
            struct['realized_pnl_dollars'] = pnl_points_to_dollars(struct['realized_pnl'], qty)


def _leg_intrinsic(leg: dict, spot: float) -> float:
    parsed = parse_canonical(str(leg.get('symbol', '')))
    if not parsed:
        return 0.0
    _, opt_type, strike = parsed
    qty = int(leg.get('qty') or leg.get('quantity') or 1)
    action = str(leg.get('action', ''))
    if opt_type == 'C':
        intrinsic = max(0.0, spot - strike)
    else:
        intrinsic = max(0.0, strike - spot)
    sign = 1 if action.startswith('BUY') else -1
    return sign * intrinsic * qty


def structure_settlement_pnl(structure: dict, spot: float) -> float:
    """Compute expiration settlement P/L for a vertical spread structure."""
    legs = structure.get('legs') or []
    settlement_value = sum(_leg_intrinsic(leg, spot) for leg in legs)
    entry_credit = float(structure.get('net_credit') or 0)
    # Credit spread: PnL = credit received + payoff (payoff is <= 0 for short spread)
    return round(entry_credit + settlement_value, 2)


def update_unrealized_marks(trade: dict, mids: Dict[str, float]) -> float:
    """Update running mark-to-market using current option mids.

    For a credit spread:
        current_mark = short_mid - long_mid (cost to buy back)
        unrealized_pnl = entry_credit - current_mark
    """
    total_unrealized = 0.0
    for struct in trade.get('structures') or []:
        if struct.get('status') in ('closed', 'cancelled'):
            continue
        short_mid = None
        long_mid = None
        for leg in struct.get('legs') or []:
            sym = str(leg.get('symbol', ''))
            mid = mids.get(sym)
            if mid is None:
                continue
            act = str(leg.get('action', ''))
            if act.startswith('SELL'):
                short_mid = mid
            elif act.startswith('BUY'):
                long_mid = mid

        if short_mid is not None and long_mid is not None:
            mark = round(short_mid - long_mid, 2)
            entry_credit = float(struct.get('net_credit') or trade.get('entry_credit') or 0.0)
            pnl = round(entry_credit - mark, 2)
            struct['current_mark'] = mark
            struct['unrealized_pnl'] = pnl
            total_unrealized += pnl

    trade['current_mark'] = mark if 'mark' in locals() else trade.get('current_mark')
    trade['unrealized_pnl'] = round(total_unrealized, 2)
    qty = int(trade.get('quantity') or 1)
    trade['unrealized_pnl_dollars'] = pnl_points_to_dollars(trade['unrealized_pnl'], qty)
    return total_unrealized


def settle_open_trade(trade_path: str, root: str, *, for_date: Optional[date] = None) -> bool:
    """Settle an open Mean Reversion trade against daily close and move to history."""
    try:
        with open(trade_path, encoding='utf-8') as f:
            trade = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False

    if trade.get('status') == 'closed':
        return False

    d = for_date or date.today()
    resolved = resolve_daily_close(root, d)
    if not resolved:
        return False
    spot, source = resolved

    total_pnl = 0.0
    for struct in trade.get('structures') or []:
        if struct.get('status') in ('cancelled',):
            continue
        pnl = structure_settlement_pnl(struct, spot)
        struct['realized_pnl'] = pnl
        struct['status'] = 'closed'
        total_pnl += pnl

    trade['status'] = 'closed'
    trade['exit_reason'] = 'eod_settlement'
    trade['settlement'] = {'spot': spot, 'source': source, 'date': d.isoformat()}
    trade['realized_pnl'] = round(total_pnl, 2)
    trade['unrealized_pnl'] = 0.0
    attach_dollar_pnl_display(trade)

    strategy = trade.get('strategy', 'MEAN_REVERSION_SPX')
    hist_dir = history_dir_for_strategy(strategy, root)
    os.makedirs(hist_dir, exist_ok=True)
    dest = os.path.join(hist_dir, os.path.basename(trade_path))
    tmp = f'{dest}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(trade, f, indent=2)
        f.flush()
    os.replace(tmp, dest)
    os.remove(trade_path)
    return True
