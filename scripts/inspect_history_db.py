#!/usr/bin/env python3
import os, sys
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)
from datetime import date
from dashboard.db import get_trades, get_daily_breakdown
from dashboard.history_sync import sync_history_from_disk

sync_history_from_disk(ROOT, for_date=date(2026, 7, 1))
rows = get_trades(date='2026-07-01')
print('=== SQLite trades 2026-07-01 ===')
meic = manual = 0
for r in rows:
    p = float(r.get('pnl') or 0)
    if r.get('strategy') == 'MANUAL_SPREAD':
        manual += p
    else:
        meic += p
    print(f"  {r['lot']:8} {r['side']} {r['status']:6} credit={r['open_credit']} close={r['close_debit']} pnl={p:8.2f} {r['strategy']}")
print(f'MEIC sum: {meic:.2f}  Manual sum: {manual:.2f}  Total: {meic+manual:.2f}')
bd = get_daily_breakdown(days=5)
for d in bd:
    if d['date'] == '2026-07-01':
        print('Breakdown:', d)
