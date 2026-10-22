"""
Generate a consolidated daily P&L spreadsheet (CSV) across all trading strategies and sub-strategies.
Combines historical records from SQLite (meic_trades.db) and Discretionary Clone Trades (trades.jsonl).
"""
from __future__ import annotations

import csv
import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


def get_sub_strategy(strat: str, lot: str) -> str:
    """Infer the specific sub-strategy variant from strategy name and lot."""
    l = (lot or '').lower()
    if strat == 'DRMA_HM_SPX':
        if l.startswith('fut_'):
            return 'FUTURES'
        if l.startswith('dir_'):
            return 'DIRECTIONAL'
        if l.startswith(('credit_', 'ic_')):
            return 'NON_DIRECTIONAL'
        return 'DRMA_GENERAL'
    if strat == 'MEIC_IC':
        return 'IRON_CONDOR'
    if strat == 'MANUAL_SPREAD':
        return 'MANUAL_SPREAD'
    if strat == 'PERMA_BULL_PCS':
        return 'BULL_PCS'
    if strat == 'SKYLINE_SPX':
        return 'BUTTERFLY'
    if strat in ('IRON_FLY_SPX', 'IRON_FLY_XSP'):
        return 'IRON_FLY'
    if strat == 'MEAN_REVERSION_SPX':
        return 'MEAN_REVERSION'
    if strat == 'CLONE_TRADES':
        return 'DISCRETIONARY'
    return 'GENERAL'


def build_daily_strategy_summary(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Aggregate closed trades by (date, strategy, sub_strategy, mode) into sorted rows."""
    if not root:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    db_path = os.path.join(root, 'dashboard', 'meic_trades.db')
    groups: Dict[tuple, Dict[str, Any]] = {}

    # 1. Query SQLite closed trades
    if os.path.isfile(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT date_opened, strategy, lot, paper, pnl, (pnl > 0) AS win
                FROM trades
                WHERE status = 'CLOSED'
                ORDER BY date_opened DESC
            """)
            for r in cur.fetchall():
                d = r['date_opened']
                if not d:
                    continue
                strat = r['strategy'] or 'MEIC_IC'
                sub = get_sub_strategy(strat, r['lot'])
                mode = 'paper' if r['paper'] in (1, True, '1', 'true') else 'live'
                pnl = float(r['pnl'] or 0.0)
                win = 1 if r['win'] else 0
                key = (d, strat, sub, mode)
                if key not in groups:
                    groups[key] = {
                        'date': d,
                        'strategy': strat,
                        'sub_strategy': sub,
                        'mode': mode,
                        'daily_pnl': 0.0,
                        'trades': 0,
                        'wins': 0,
                    }
                groups[key]['daily_pnl'] = round(groups[key]['daily_pnl'] + pnl, 2)
                groups[key]['trades'] += 1
                groups[key]['wins'] += win
            conn.close()
        except Exception as e:
            log.warning('Could not query meic_trades.db for daily summary: %s', e)

    # 2. Add Discretionary Clone Trades from journal
    clone_journal = os.path.join(root, 'trades', 'clone_trades', 'trades.jsonl')
    if os.path.isfile(clone_journal):
        try:
            with open(clone_journal, encoding='utf-8') as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        t = json.loads(line)
                        if t.get('status') == 'CLOSED':
                            d = t.get('date_cst') or (t.get('closed_at_cst') or '')[:10]
                            if not d:
                                continue
                            pnl = float(t.get('realized_pnl_usd') or 0.0)
                            win = 1 if pnl > 0 else 0
                            mode = 'paper' if t.get('paper') in (1, True, '1', 'true') else 'live'
                            key = (d, 'CLONE_TRADES', 'DISCRETIONARY', mode)
                            if key not in groups:
                                groups[key] = {
                                    'date': d,
                                    'strategy': 'CLONE_TRADES',
                                    'sub_strategy': 'DISCRETIONARY',
                                    'mode': mode,
                                    'daily_pnl': 0.0,
                                    'trades': 0,
                                    'wins': 0,
                                }
                            groups[key]['daily_pnl'] = round(groups[key]['daily_pnl'] + pnl, 2)
                            groups[key]['trades'] += 1
                            groups[key]['wins'] += win
                    except Exception:
                        pass
        except Exception as e:
            log.warning('Could not read clone_trades journal for daily summary: %s', e)

    results: List[Dict[str, Any]] = []
    for k in sorted(groups.keys(), key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True):
        row = groups[k]
        trades = row['trades']
        wins = row['wins']
        row['win_rate_pct'] = round((wins / trades * 100.0), 1) if trades > 0 else 0.0
        results.append(row)

    return results


def export_strategy_daily_pnl_csv(
    root: Optional[str] = None,
    out_path: Optional[str] = None,
) -> str:
    """Generate and write streamlined strategy_daily_pnl.csv file."""
    if not root:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    if not out_path:
        reports_dir = os.path.join(root, 'reports')
        os.makedirs(reports_dir, exist_ok=True)
        out_path = os.path.join(reports_dir, 'strategy_daily_pnl.csv')
    else:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)

    rows = build_daily_strategy_summary(root)

    fieldnames = [
        'Date',
        'Strategy',
        'Sub_Strategy',
        'Mode',
        'Daily_PnL',
        'Trades',
        'Wins',
        'Win_Rate_Pct',
    ]

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                'Date': r['date'],
                'Strategy': r['strategy'],
                'Sub_Strategy': r['sub_strategy'],
                'Mode': r['mode'],
                'Daily_PnL': f"{r['daily_pnl']:.2f}",
                'Trades': r['trades'],
                'Wins': r['wins'],
                'Win_Rate_Pct': f"{r['win_rate_pct']:.1f}%",
            })

    log.info('Wrote daily strategy P&L spreadsheet to %s (%d rows)', out_path, len(rows))
    return out_path
