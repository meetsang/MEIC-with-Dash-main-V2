"""Settlement and unrealized PnL calculation for DRMA Stocks trades."""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


def update_stock_unrealized_marks(
    trade: Dict[str, Any],
    current_spot: float,
    option_mids: Optional[Dict[str, float]] = None,
) -> None:
    """Update unrealized PnL for open stock equity shares or option spread trades."""
    if current_spot <= 0:
        return

    trade['current_spot'] = round(current_spot, 2)
    entry_spot = float(trade.get('underlying_entry_price') or trade.get('entry_price') or current_spot)
    structure = str(trade.get('structure_type') or trade.get('trade_type') or '').lower()

    if 'stock' in structure or 'equity' in structure:
        # Direct stock buy / sell (shares)
        shares = int(trade.get('quantity') or trade.get('shares') or 1)
        entry_px = float(trade.get('entry_price') or current_spot)
        is_long = (str(trade.get('side', 'LONG')).upper() == 'LONG')

        if is_long:
            pnl = round((current_spot - entry_px) * shares, 2)
        else:
            pnl = round((entry_px - current_spot) * shares, 2)

        trade['current_mark'] = round(current_spot, 2)
        trade['unrealized_pnl_dollars'] = pnl
        trade['unrealized_pnl'] = pnl
        trade['realized_pnl_dollars'] = pnl if trade.get('status') == 'closed' else 0.0
    else:
        # Options Spread (debit or credit spread)
        qty = int(trade.get('quantity') or 1)
        entry_price = float(trade.get('entry_price') or 1.0)
        is_debit = ('debit' in structure or 'directional' in structure)
        side = str(trade.get('opt_type') or 'C').upper()
        spot_delta = current_spot - entry_spot

        # If live broker mids provided for legs
        if option_mids:
            long_mid = 0.0
            short_mid = 0.0
            for struct in trade.get('structures') or []:
                for leg in struct.get('legs') or []:
                    sym = leg.get('symbol')
                    act = str(leg.get('action') or '').upper()
                    mid = option_mids.get(sym, 0.0)
                    if 'BUY' in act:
                        long_mid += mid
                    elif 'SELL' in act:
                        short_mid += mid
            if long_mid > 0 or short_mid > 0:
                current_mark = round(long_mid - short_mid if is_debit else short_mid - long_mid, 2)
                pnl = round((current_mark - entry_price) * 100 * qty if is_debit else (entry_price - current_mark) * 100 * qty, 2)
                trade['current_mark'] = current_mark
                trade['unrealized_pnl_dollars'] = pnl
                trade['unrealized_pnl'] = pnl
                return

        # Synthetic delta-model estimate based on spot move
        est_delta = 0.40 if is_debit else 0.15
        if side == 'P':
            est_delta = -est_delta
        option_move = spot_delta * est_delta
        current_mark = max(0.05, round(entry_price + option_move if is_debit else entry_price - option_move, 2))
        pnl = round((current_mark - entry_price) * 100 * qty if is_debit else (entry_price - current_mark) * 100 * qty, 2)
        trade['current_mark'] = current_mark
        trade['unrealized_pnl_dollars'] = pnl
        trade['unrealized_pnl'] = pnl


def settle_stock_trade(
    trade: Dict[str, Any],
    exit_price: float,
    exit_reason: str,
) -> None:
    """Close trade, freeze realized PnL, and set timestamps."""
    structure = str(trade.get('structure_type') or '').lower()
    trade['status'] = 'closed'
    trade['closed_at'] = datetime.now().isoformat()
    trade['exit_price'] = round(exit_price, 2)
    trade['exit_reason'] = exit_reason

    if 'stock' in structure or 'equity' in structure:
        shares = int(trade.get('quantity') or trade.get('shares') or 1)
        entry_px = float(trade.get('entry_price') or exit_price)
        is_long = (str(trade.get('side', 'LONG')).upper() == 'LONG')
        realized = round((exit_price - entry_px) * shares if is_long else (entry_px - exit_price) * shares, 2)
    else:
        qty = int(trade.get('quantity') or 1)
        entry_px = float(trade.get('entry_price') or exit_price)
        is_debit = ('debit' in structure)
        realized = round((exit_price - entry_px) * 100 * qty if is_debit else (entry_px - exit_price) * 100 * qty, 2)

    trade['realized_pnl_dollars'] = realized
    trade['realized_pnl'] = realized
    trade['unrealized_pnl_dollars'] = 0.0
    trade['unrealized_pnl'] = 0.0


def move_trade_to_history(active_path: str, history_dir: str) -> Optional[str]:
    """Move a settled trade JSON file from active directory to history directory."""
    if not os.path.isfile(active_path):
        return None
    try:
        os.makedirs(history_dir, exist_ok=True)
        fname = os.path.basename(active_path)
        dest_path = os.path.join(history_dir, fname)
        shutil.move(active_path, dest_path)
        log.info("Moved completed trade to history: %s -> %s", active_path, dest_path)
        return dest_path
    except Exception as exc:
        log.error("Failed to move trade to history (%s): %s", active_path, exc)
        return None
