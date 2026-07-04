#!/usr/bin/env python3
"""One-off: reconcile Jul 1 net PnL from trade JSON."""
from __future__ import annotations

import glob
import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from dashboard.server import _trade_pnl  # noqa: E402

SPX_BUG = 7505.0


def pnl_from_trade(t, cur_short=None, cur_long=None):
    entry = t.get('entry', {})
    short_leg = t.get('short_leg', {})
    long_leg = t.get('long_leg', {})
    net = float(entry.get('net_credit') or 0)
    qty = int(t.get('filled_quantity') or t.get('quantity') or 1)
    sf = float(short_leg.get('fill_price') or 0)
    lf = float(long_leg.get('fill_price') or 0)
    sc = t.get('short_close_price')
    lc = t.get('long_close_price')
    cs = float(cur_short if cur_short is not None else (sc if sc is not None else sf))
    cl = float(cur_long if cur_long is not None else (lc if lc is not None else lf))
    pnl, exit_cr, _, frozen = _trade_pnl(
        net, qty, sf, lf, sc, lc, cs, cl, t.get('status', ''), trade=t,
    )
    return pnl, frozen, exit_cr, net, qty, cs, cl


def collect_trades():
    patterns = [
        'trades/history/MEIC_IC/2026-07-01/*.json',
        'trades/history/MEIC_IC/*20260701*.json',
        'trades/history/MANUAL_SPREAD/2026-07-01/*.json',
        'trades/history/MANUAL_SPREAD/*20260701*.json',
        'trades/active/MANUAL_SPREAD/*20260701*.json',
        'trades/active/MEIC_IC/*20260701*.json',
    ]
    seen = set()
    out = []
    for pat in patterns:
        for p in sorted(glob.glob(os.path.join(ROOT, pat))):
            b = os.path.basename(p)
            if b in seen:
                continue
            seen.add(b)
            with open(p, encoding='utf-8') as f:
                t = json.load(f)
            t['_file'] = b
            out.append(t)
    return out


def main():
    trades = collect_trades()
    meic_closed = meic_open = manual_closed = manual_open = 0.0

    print('=== Per-trade PnL (dashboard formula) ===')
    for t in trades:
        entry = t.get('entry', {})
        strat = entry.get('strategy', '?')
        lot = t.get('lot', '?')
        side = entry.get('side', '?')
        st = t.get('status', '?')
        filled = int(t.get('filled_quantity') or 0)

        pnl, frozen, exit_cr, net, qty, cs, cl = pnl_from_trade(t)
        pnl_bug = pnl
        if st in ('open', 'closing') and filled > 0:
            lf = float(t.get('long_leg', {}).get('fill_price') or 0.15)
            pnl_bug, _, _, _, _, _, _ = pnl_from_trade(t, cur_short=SPX_BUG, cur_long=lf)

        if filled <= 0:
            print(f'{t["_file"]:42} {lot:6} {side} {st:14} — no fill')
            continue

        line = (
            f'{t["_file"]:42} {lot:6} {side} {st:14} '
            f'credit={net:.2f} qty={qty} pnl={pnl:9.2f} frozen={frozen} exit_spread={exit_cr}'
        )
        if st in ('open', 'closing'):
            line += f' spx_bug_pnl={pnl_bug:9.2f}'
        print(line)

        is_manual = strat == 'MANUAL_SPREAD'
        if st == 'closed':
            if is_manual:
                manual_closed += pnl
            else:
                meic_closed += pnl
        elif st in ('open', 'closing'):
            if is_manual:
                manual_open += pnl
            else:
                meic_open += pnl

    print()
    print(f'MEIC  closed PnL:  ${meic_closed:,.2f}')
    print(f'MEIC  open PnL (mark=fill): ${meic_open:,.2f}')
    print(f'MANUAL closed PnL: ${manual_closed:,.2f}')
    print(f'MANUAL open PnL (mark=fill): ${manual_open:,.2f}')
    print(f'Combined closed: ${meic_closed + manual_closed:,.2f}')
    print(f'Combined all (open at fill mark): ${meic_closed + manual_closed + meic_open + manual_open:,.2f}')

    print()
    print('=== SPX-fallback bug on OPEN manual (if MQTT missing option mid) ===')
    bug_total = 0.0
    for t in trades:
        if t.get('status') not in ('open', 'closing'):
            continue
        if t.get('entry', {}).get('strategy') != 'MANUAL_SPREAD':
            continue
        if int(t.get('filled_quantity') or 0) <= 0:
            continue
        lf = float(t.get('long_leg', {}).get('fill_price') or 0.15)
        pnl_bug, _, _, _, _, _, _ = pnl_from_trade(t, cur_short=SPX_BUG, cur_long=lf)
        bug_total += pnl_bug
        print(f'  {t.get("lot")} {t.get("entry",{}).get("side")} qty={t.get("filled_quantity")} -> ${pnl_bug:,.2f}')
    print(f'  Manual open total under SPX bug: ${bug_total:,.2f}')


if __name__ == '__main__':
    main()
