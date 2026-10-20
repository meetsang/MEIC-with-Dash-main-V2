"""Settlement, mark-to-market tracking, and PnL calculation for DRMA_HM."""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Dict, Optional

from common.expiry_settlement import resolve_daily_close
from common.symbols import parse_canonical
from common.trades_layout import history_dir_for_strategy

log = logging.getLogger(__name__)

SPX_OPTION_MULTIPLIER = 100
ES_FUTURES_MULTIPLIER = 50.0


def pnl_points_to_dollars(pnl_points: float, quantity: int = 1, multiplier: Optional[float] = None) -> float:
    """Convert points to dollars (default SPX option multiplier ×100, or custom e.g. ×50 for ES)."""
    mult = multiplier if multiplier is not None else SPX_OPTION_MULTIPLIER
    return round(float(pnl_points) * float(mult) * int(quantity or 1), 2)


def attach_dollar_pnl_display(trade: dict) -> None:
    """Add *_dollars fields alongside point P/L on a DRMA_HM trade dict."""
    qty = int(trade.get('quantity') or 1)
    is_futures = (
        str(trade.get('structure_type', '')).lower() == 'futures'
        or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES'
        or str(trade.get('symbol', '')).upper() in ('ES', '/ES')
    )
    multiplier = float(trade.get('multiplier') or (ES_FUTURES_MULTIPLIER if is_futures else SPX_OPTION_MULTIPLIER))

    if trade.get('unrealized_pnl') is not None:
        trade['unrealized_pnl_dollars'] = pnl_points_to_dollars(trade['unrealized_pnl'], qty, multiplier=multiplier)
    if trade.get('realized_pnl') is not None:
        trade['realized_pnl_dollars'] = pnl_points_to_dollars(trade['realized_pnl'], qty, multiplier=multiplier)
    for struct in trade.get('structures') or []:
        s_mult = float(struct.get('multiplier') or multiplier)
        if struct.get('unrealized_pnl') is not None:
            struct['unrealized_pnl_dollars'] = pnl_points_to_dollars(struct['unrealized_pnl'], qty, multiplier=s_mult)
        if struct.get('realized_pnl') is not None:
            struct['realized_pnl_dollars'] = pnl_points_to_dollars(struct['realized_pnl'], qty, multiplier=s_mult)


def update_futures_unrealized_marks(trade: dict, current_es_price: float) -> float:
    """Update running mark-to-market for outright ES directional futures trade.

    For Long ES:
        pnl_pts = current_es_price - entry_price
    For Short ES:
        pnl_pts = entry_price - current_es_price
    """
    if current_es_price is None or current_es_price <= 0:
        return float(trade.get('unrealized_pnl') or 0.0)

    entry_price = float(trade.get('entry_price') or current_es_price)
    side = str(trade.get('side', 'LONG')).upper()
    if side == 'LONG':
        pnl_pts = round(current_es_price - entry_price, 2)
    else:
        pnl_pts = round(entry_price - current_es_price, 2)

    multiplier = float(trade.get('multiplier') or ES_FUTURES_MULTIPLIER)
    qty = int(trade.get('quantity') or 1)
    dollar_pnl = round(pnl_pts * multiplier * qty, 2)

    trade['current_mark'] = current_es_price
    trade['unrealized_pnl'] = pnl_pts
    trade['unrealized_pnl_dollars'] = dollar_pnl

    for struct in trade.get('structures') or []:
        struct['current_mark'] = current_es_price
        struct['unrealized_pnl'] = pnl_pts
        struct['unrealized_pnl_dollars'] = dollar_pnl

    return pnl_pts


def update_unrealized_marks(trade: dict, mids: Dict[str, float]) -> float:
    """Update running mark-to-market using current option mids.

    For a credit spread:
        current_mark = short_mid - long_mid (cost to buy back)
        unrealized_pnl = entry_credit - current_mark
    For a debit spread:
        current_mark = long_mid - short_mid (value to liquidate)
        unrealized_pnl = current_mark - entry_debit
    """
    if not mids:
        return float(trade.get('unrealized_pnl') or 0.0)

    total_unrealized = 0.0
    structure_type = str(trade.get('structure_type', 'credit')).lower()

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
            if 'debit' in structure_type:
                # Debit spread
                mark = round(long_mid - short_mid, 2)
                entry_cost = float(struct.get('net_debit') or trade.get('entry_price') or 0.0)
                pnl = round(mark - entry_cost, 2)
            else:
                # Credit spread
                mark = round(short_mid - long_mid, 2)
                entry_credit = float(struct.get('net_credit') or trade.get('entry_price') or 0.0)
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
    """Settle an open DRMA_HM trade against daily close and move to history."""
    try:
        if not os.path.isfile(trade_path):
            return False
        with open(trade_path, encoding='utf-8') as f:
            trade = json.load(f)

        if trade.get('status') == 'closed':
            return False

        today = for_date or date.today()
        resolved = resolve_daily_close(root, today)
        if not resolved:
            log.warning('Cannot settle DRMA_HM trade: closing SPX spot unavailable')
            return False
        close_spot, close_source = resolved

        structure_type = str(trade.get('structure_type', 'credit')).lower()
        qty = int(trade.get('quantity') or 1)
        total_pnl = 0.0

        if structure_type == 'futures' or str(trade.get('trade_type', '')).upper() == 'DIRECTIONAL_FUTURES':
            multiplier = float(trade.get('multiplier') or ES_FUTURES_MULTIPLIER)
            entry_price = float(trade.get('entry_price') or close_spot)
            settle_px = float(trade.get('current_mark') or close_spot)
            side = str(trade.get('side', 'LONG')).upper()
            pnl_pts = round((settle_px - entry_price) if side == 'LONG' else (entry_price - settle_px), 2)
            total_pnl = pnl_pts
            for struct in trade.get('structures') or []:
                struct['realized_pnl'] = pnl_pts
                struct['realized_pnl_dollars'] = round(pnl_pts * multiplier * qty, 2)
                struct['status'] = 'closed'
            trade['realized_pnl'] = round(total_pnl, 2)
            trade['realized_pnl_dollars'] = pnl_points_to_dollars(trade['realized_pnl'], qty, multiplier=multiplier)
        else:
            for struct in trade.get('structures') or []:
                legs = struct.get('legs') or []
                settlement_value = 0.0
                for leg in legs:
                    parsed = parse_canonical(str(leg.get('symbol', '')))
                    if not parsed:
                        continue
                    _, opt_type, strike = parsed
                    act = str(leg.get('action', ''))
                    intrinsic = max(0.0, close_spot - strike) if opt_type == 'C' else max(0.0, strike - close_spot)
                    sign = 1 if 'BUY' in act else -1
                    settlement_value += sign * intrinsic

                if 'debit' in structure_type:
                    entry_debit = float(struct.get('net_debit') or trade.get('entry_price') or 0.0)
                    pnl = round(settlement_value - entry_debit, 2)
                else:
                    entry_credit = float(struct.get('net_credit') or trade.get('entry_price') or 0.0)
                    pnl = round(entry_credit + settlement_value, 2)

                struct['realized_pnl'] = pnl
                struct['status'] = 'closed'
                total_pnl += pnl

            trade['realized_pnl'] = round(total_pnl, 2)
            trade['realized_pnl_dollars'] = pnl_points_to_dollars(trade['realized_pnl'], qty, multiplier=SPX_OPTION_MULTIPLIER)
        trade['unrealized_pnl'] = 0.0
        trade['unrealized_pnl_dollars'] = 0.0
        trade['status'] = 'closed'
        trade['exit_reason'] = 'settled_at_expiry'
        trade['close_reason'] = 'settled_at_expiry'
        trade['exited_at'] = trade.get('exited_at') or f"{today.isoformat()}T15:00:00"
        trade['closing_spot'] = close_spot

        hist_dir = history_dir_for_strategy(trade.get('strategy', 'DRMA_HM_SPX'), root)
        os.makedirs(hist_dir, exist_ok=True)
        filename = os.path.basename(trade_path)
        hist_path = os.path.join(hist_dir, filename)

        with open(hist_path, 'w', encoding='utf-8') as f:
            json.dump(trade, f, indent=2)

        try:
            os.remove(trade_path)
        except OSError:
            pass

        log.info('Settled and archived DRMA_HM trade %s (pnl=$%.2f)', filename, trade['realized_pnl_dollars'])
        return True
    except Exception as exc:
        log.error('Failed to settle DRMA_HM trade: %s', exc)
        return False
