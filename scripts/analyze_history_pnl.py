"""Analyze history JSON PnL by close type."""
from __future__ import annotations

import json
import glob
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from common.expiry_settlement import compute_otm_decay_pnl, compute_settled_pnl, has_real_close_fills


def actual_pnl(t):
    entry = t.get('entry') or {}
    cr = float(entry.get('net_credit') or 0)
    qty = int(t.get('filled_quantity') or t.get('quantity') or 1)
    sc = t.get('short_close_price')
    lc = t.get('long_close_price')
    if t.get('status') != 'closed' or sc is None or lc is None:
        return None
    debit = float(sc) - float(lc)
    spread_type = (t.get('spread_type') or 'credit').lower()
    if spread_type == 'debit':
        return round((debit - cr) * 100 * qty, 2)
    return round((cr - debit) * 100 * qty, 2)


def decay_pnl(t):
    if has_real_close_fills(t):
        settled = compute_settled_pnl(t, 0.0)
        return settled['pnl'] if settled else None
    settled = compute_otm_decay_pnl(t)
    return settled['pnl'] if settled else None


def summarize(date: str):
    paths = []
    for base in ('MEIC_IC', 'MANUAL_SPREAD'):
        paths.extend(glob.glob(os.path.join(ROOT, 'trades', 'history', base, '**', '*.json'), recursive=True))
    by_entry = []
    for path in paths:
        with open(path, encoding='utf-8') as f:
            t = json.load(f)
        ts = (t.get('entry') or {}).get('timestamp', '')[:10]
        if ts == date:
            by_entry.append((path, t))
    print(f'DATE {date} trades {len(by_entry)}')
    stop_sum = open_sum = 0.0
    for path, t in sorted(by_entry, key=lambda x: (x[1].get('lot') or '', (x[1].get('entry') or {}).get('side') or '')):
        cm = t.get('close_mechanism') or (t.get('close') or {}).get('reason')
        act = actual_pnl(t)
        dec = decay_pnl(t)
        st = t.get('status')
        if st == 'open' or act is None:
            kind = 'OPEN'
            open_sum += dec or 0
        elif cm == 'exchange_stop':
            kind = 'STOP'
            stop_sum += act or 0
        else:
            kind = 'CLOSED'
            open_sum += act or 0
        lot = t.get('lot')
        side = (t.get('entry') or {}).get('side')
        print(f'  {lot:8} {side} {kind:6} actual={act} decay={dec} cm={cm}')
    print(f'  STOP total={stop_sum:.2f}  OPEN/decay total={open_sum:.2f}  combined={stop_sum+open_sum:.2f}')
    print()


if __name__ == '__main__':
    for d in ('2026-06-29', '2026-06-26', '2026-06-25', '2026-06-24'):
        summarize(d)
