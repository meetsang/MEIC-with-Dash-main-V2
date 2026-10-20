"""Convert strategy-specific trade JSON into dashboard history rows."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from common import trades_layout

SKIP_ACTIVE_JSON_PREFIXES = ('session_', 'halted_')


def is_syncable_trade_path(path: str) -> bool:
    base = os.path.basename(path)
    if not base.endswith('.json'):
        return False
    return not any(base.startswith(prefix) for prefix in SKIP_ACTIVE_JSON_PREFIXES)


def is_skyline_trade(trade: Dict[str, Any]) -> bool:
    return bool(trade.get('structures')) and trade.get('strategy') == trades_layout.STRATEGY_SKYLINE


def skyline_trade_to_history_row(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a settled Skyline trade JSON to one dashboard history row."""
    date_opened = str(trade.get('date') or '')
    if not date_opened:
        return None

    structures = trade.get('structures') or []
    active = [
        s for s in structures
        if s.get('status') not in ('cancelled',) and s.get('legs')
    ]
    if not active and trade.get('skip_reason'):
        return None

    status = str(trade.get('status') or 'open').upper()
    if status not in ('CLOSED', 'OPEN'):
        status = 'CLOSED' if trade.get('settlement') else 'OPEN'

    if status != 'CLOSED' and trade.get('realized_pnl_total') is None:
        return None

    qty = int(trade.get('quantity') or 1)
    net_debit = float(trade.get('net_debit_total') or 0)
    pnl_raw = trade.get('realized_pnl_total')
    pnl = None
    if pnl_raw is not None:
        from strategies.skyline.settlement import pnl_points_to_dollars
        pnl = pnl_points_to_dollars(float(pnl_raw), qty)

    names = ','.join(s.get('name', '') for s in active if s.get('name'))
    atm = trade.get('atm', '')

    return {
        'date_opened': date_opened,
        'time_opened': trade.get('entry_time_et', ''),
        'strategy': trades_layout.STRATEGY_SKYLINE,
        'lot': f'skyline-{date_opened}',
        'side': 'X',
        'short_symbol': f'ATM{atm}' + (f' [{names}]' if names else ''),
        'long_symbol': '',
        'quantity': qty,
        'filled_price': round(-net_debit, 4) if net_debit else 0.0,
        'short_open_price': 0.0,
        'long_open_price': 0.0,
        'short_close_price': None,
        'long_close_price': None,
        'close_debit': None,
        'pnl': pnl,
        'status': status,
        'open_order_id': '',
        'short_close_order_id': '',
        'paper': 1 if bool(trade.get('paper', True)) else 0,
    }


def is_drma_hm_trade(trade: Dict[str, Any]) -> bool:
    strat = trade.get('strategy')
    return strat == trades_layout.STRATEGY_DRMA_HM or str(trade.get('trade_id', '')).startswith(('dir_', 'credit_', 'ic_'))


def drma_hm_trade_to_history_row(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a DRMA + Hilega-Milega trade JSON to a dashboard history row."""
    entered_at = trade.get('entered_at') or ''
    date_opened = entered_at[:10] if len(entered_at) >= 10 else str(trade.get('date') or '')
    if not date_opened:
        return None

    trade_id = trade.get('trade_id') or f"drma_hm_{date_opened}"
    status = str(trade.get('status') or 'open').upper()
    qty = int(trade.get('quantity') or 1)
    fill = float(trade.get('entry_price') or trade.get('net_credit') or trade.get('net_debit') or 0.0)
    exit_p = trade.get('exit_price')
    close_debit = float(exit_p) if exit_p is not None else None

    pnl = trade.get('realized_pnl_dollars')
    if pnl is None and trade.get('realized_pnl') is not None:
        pnl = round(float(trade['realized_pnl']) * 100 * qty, 2)

    side = str(trade.get('opt_type') or ('C' if 'bull' in trade_id else 'P')).upper()
    short_sym = ''
    long_sym = ''
    structs = trade.get('structures') or []
    if structs:
        legs = structs[0].get('legs') or []
        for leg in legs:
            action = str(leg.get('action') or '').upper()
            if 'SELL' in action:
                short_sym = leg.get('symbol') or f"K{leg.get('strike')}"
            elif 'BUY' in action:
                long_sym = leg.get('symbol') or f"K{leg.get('strike')}"

    return {
        'date_opened': date_opened,
        'time_opened': entered_at,
        'strategy': trades_layout.STRATEGY_DRMA_HM,
        'lot': trade_id,
        'side': side,
        'short_symbol': short_sym,
        'long_symbol': long_sym,
        'quantity': qty,
        'open_credit': fill,
        'short_open_price': fill,
        'long_open_price': 0.0,
        'short_close_price': close_debit,
        'long_close_price': 0.0,
        'close_debit': close_debit,
        'pnl': pnl,
        'status': status,
        'open_order_id': trade.get('open_order_id', ''),
        'short_close_order_id': trade.get('close_order_id', ''),
        'paper': 1 if bool(trade.get('paper', True)) else 0,
    }


def is_iron_fly_trade(trade: Dict[str, Any]) -> bool:
    strat = trade.get('strategy')
    return strat in (trades_layout.STRATEGY_IRON_FLY, trades_layout.STRATEGY_IRON_FLY_XSP) or bool(trade.get('structures') and 'IF_' in str(trade.get('structures', [{}])[0].get('name', '')))


def iron_fly_trade_to_history_row(trade: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Map a settled Iron Fly book JSON to one dashboard history row."""
    date_opened = str(trade.get('date') or '')
    if not date_opened:
        return None

    strat = trade.get('strategy') or trades_layout.STRATEGY_IRON_FLY
    pnl = trade.get('realized_pnl', trade.get('realized_pnl_total', trade.get('pnl')))
    status = str(trade.get('status') or 'open').upper()
    if pnl is not None and status != 'CLOSED':
        status = 'CLOSED'

    structs = trade.get('structures') or []
    lot = f"{strat.lower()}-{date_opened}"

    return {
        'date_opened': date_opened,
        'time_opened': date_opened,
        'strategy': strat,
        'lot': lot,
        'side': 'IF',
        'short_symbol': f"{len(structs)} flies" if structs else 'Iron Fly',
        'long_symbol': '',
        'quantity': len(structs) or 1,
        'open_credit': 0.0,
        'short_open_price': 0.0,
        'long_open_price': 0.0,
        'short_close_price': None,
        'long_close_price': None,
        'close_debit': None,
        'pnl': float(pnl) if pnl is not None else None,
        'status': status,
        'open_order_id': '',
        'short_close_order_id': '',
        'paper': 1 if bool(trade.get('paper', True)) else 0,
    }


def production_strategy_dirs() -> List[tuple[str, str, str]]:
    return [
        (trades_layout.STRATEGY_MEIC, trades_layout.MEIC_ACTIVE, trades_layout.MEIC_HISTORY),
        (trades_layout.STRATEGY_MANUAL, trades_layout.MANUAL_ACTIVE, trades_layout.MANUAL_HISTORY),
        (trades_layout.STRATEGY_SKYLINE, trades_layout.SKYLINE_ACTIVE, trades_layout.SKYLINE_HISTORY),
        (trades_layout.STRATEGY_DRMA_HM, trades_layout.DRMA_HM_ACTIVE, trades_layout.DRMA_HM_HISTORY),
        (trades_layout.STRATEGY_IRON_FLY, trades_layout.IRON_FLY_ACTIVE, trades_layout.IRON_FLY_HISTORY),
        (trades_layout.STRATEGY_IRON_FLY_XSP, trades_layout.IRON_FLY_XSP_ACTIVE, trades_layout.IRON_FLY_XSP_HISTORY),
        (trades_layout.STRATEGY_PERMA_BULL, trades_layout.PERMA_BULL_ACTIVE, trades_layout.PERMA_BULL_HISTORY),
        (trades_layout.STRATEGY_MEAN_REVERSION, trades_layout.MEAN_REVERSION_ACTIVE, trades_layout.MEAN_REVERSION_HISTORY),
    ]
