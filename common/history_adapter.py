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


def production_strategy_dirs() -> List[tuple[str, str, str]]:
    return [
        (trades_layout.STRATEGY_MEIC, trades_layout.MEIC_ACTIVE, trades_layout.MEIC_HISTORY),
        (trades_layout.STRATEGY_MANUAL, trades_layout.MANUAL_ACTIVE, trades_layout.MANUAL_HISTORY),
        (trades_layout.STRATEGY_SKYLINE, trades_layout.SKYLINE_ACTIVE, trades_layout.SKYLINE_HISTORY),
    ]
