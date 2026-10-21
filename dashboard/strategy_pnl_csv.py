"""
Generate a consolidated daily P&L spreadsheet (CSV) across all trading strategies.
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

ALL_STRATEGIES = [
    'MEIC_IC',
    'MANUAL_SPREAD',
    'CLONE_TRADES',
    'SKYLINE_SPX',
    'PERMA_BULL_PCS',
    'DRMA_HM_SPX',
    'IRON_FLY_SPX',
    'IRON_FLY_XSP',
    'MEAN_REVERSION_SPX',
]


def build_daily_strategy_summary(root: Optional[str] = None) -> List[Dict[str, Any]]:
    """Aggregate closed trades by date and strategy into sorted daily summary rows."""
    if not root:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

    db_path = os.path.join(root, 'dashboard', 'meic_trades.db')
    by_date: Dict[str, Dict[str, Any]] = {}

    # 1. Query SQLite closed trades
    if os.path.isfile(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            cur.execute("""
                SELECT date_opened, strategy, pnl, (pnl > 0) AS win
                FROM trades
                WHERE status = 'CLOSED'
                ORDER BY date_opened DESC
            """)
            for r in cur.fetchall():
                d = r['date_opened']
                if not d:
                    continue
                if d not in by_date:
                    by_date[d] = {
                        'date': d,
                        'total_pnl': 0.0,
                        'total_trades': 0,
                        'wins': 0,
                        **{s: 0.0 for s in ALL_STRATEGIES},
                    }
                pnl = float(r['pnl'] or 0.0)
                strat = r['strategy'] or 'MEIC_IC'
                by_date[d]['total_pnl'] = round(by_date[d]['total_pnl'] + pnl, 2)
                by_date[d]['total_trades'] += 1
                if r['win']:
                    by_date[d]['wins'] += 1
                if strat in by_date[d]:
                    by_date[d][strat] = round(by_date[d][strat] + pnl, 2)
                else:
                    by_date[d]['CLONE_TRADES'] = round(by_date[d]['CLONE_TRADES'] + pnl, 2)
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
                            if d not in by_date:
                                by_date[d] = {
                                    'date': d,
                                    'total_pnl': 0.0,
                                    'total_trades': 0,
                                    'wins': 0,
                                    **{s: 0.0 for s in ALL_STRATEGIES},
                                }
                            by_date[d]['CLONE_TRADES'] = round(by_date[d]['CLONE_TRADES'] + pnl, 2)
                            by_date[d]['total_pnl'] = round(by_date[d]['total_pnl'] + pnl, 2)
                            by_date[d]['total_trades'] += 1
                            if pnl > 0:
                                by_date[d]['wins'] += 1
                    except Exception:
                        pass
        except Exception as e:
            log.warning('Could not read clone_trades journal for daily summary: %s', e)

    # Sort descending by date
    results: List[Dict[str, Any]] = []
    for d in sorted(by_date.keys(), reverse=True):
        row = by_date[d]
        trades = row['total_trades']
        wins = row['wins']
        win_rate = round((wins / trades * 100.0), 1) if trades > 0 else 0.0
        row['win_rate_pct'] = win_rate
        results.append(row)

    return results


def export_strategy_daily_pnl_csv(
    root: Optional[str] = None,
    out_path: Optional[str] = None,
) -> str:
    """Generate and write strategy_daily_pnl.csv spreadsheet file."""
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
        'Total_PnL',
        'Win_Rate_Pct',
        'Total_Trades',
        'MEIC_IC_PnL',
        'MANUAL_SPREAD_PnL',
        'CLONE_TRADES_PnL',
        'SKYLINE_SPX_PnL',
        'PERMA_BULL_PCS_PnL',
        'DRMA_HM_SPX_PnL',
        'IRON_FLY_SPX_PnL',
        'IRON_FLY_XSP_PnL',
        'MEAN_REVERSION_SPX_PnL',
    ]

    with open(out_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({
                'Date': r['date'],
                'Total_PnL': f"{r['total_pnl']:.2f}",
                'Win_Rate_Pct': f"{r['win_rate_pct']:.1f}%",
                'Total_Trades': r['total_trades'],
                'MEIC_IC_PnL': f"{r.get('MEIC_IC', 0.0):.2f}",
                'MANUAL_SPREAD_PnL': f"{r.get('MANUAL_SPREAD', 0.0):.2f}",
                'CLONE_TRADES_PnL': f"{r.get('CLONE_TRADES', 0.0):.2f}",
                'SKYLINE_SPX_PnL': f"{r.get('SKYLINE_SPX', 0.0):.2f}",
                'PERMA_BULL_PCS_PnL': f"{r.get('PERMA_BULL_PCS', 0.0):.2f}",
                'DRMA_HM_SPX_PnL': f"{r.get('DRMA_HM_SPX', 0.0):.2f}",
                'IRON_FLY_SPX_PnL': f"{r.get('IRON_FLY_SPX', 0.0):.2f}",
                'IRON_FLY_XSP_PnL': f"{r.get('IRON_FLY_XSP', 0.0):.2f}",
                'MEAN_REVERSION_SPX_PnL': f"{r.get('MEAN_REVERSION_SPX', 0.0):.2f}",
            })

    log.info('Wrote daily strategy P&L spreadsheet to %s (%d rows)', out_path, len(rows))
    return out_path
