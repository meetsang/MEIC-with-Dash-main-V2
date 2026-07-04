#!/usr/bin/env python3
"""Deep-dive investigation for Jun 25, 2026 paper-day incidents.

Cases:
  1. 01-15 put — overlap shift + scan candidate selection
  2. 02-00 put — ghost JSON vs real breach (dashboard)
  3. ms-8 manual 3-lot put — JSON order id vs broker fill

Run from repo root:
  python scripts/investigate_jun25_incidents.py
  python scripts/investigate_jun25_incidents.py --broker   # query TastyTrade
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

import meic0dte.app.config as meic_config
from common.strike_guard import leg_overlap_conflict
from common.symbols import build_tastytrade_symbol, to_tastytrade
from blocks.entry.spread_scan import _evaluate_spread, _round_credit
from blocks.stop import fill_sync, state as state_mod

EXPIRY = '260625'

# From meic0dte/logs/01-15_put.log @ 13:14:05 (TastyTrade REST mids)
PRICES_01_15_SCAN = {
    '.SPXW260625P7325': 2.55,
    '.SPXW260625P7300': 0.82,
    '.SPXW260625P7320': 1.98,
    '.SPXW260625P7295': 0.68,
}

# Stream mids ~13:14:06 CT (shift-target legs; P7335 estimated from P7325 − ~0.45)
PRICES_01_15_SHIFT_EST = {
    '.SPXW260625P7335': 2.10,
    '.SPXW260625P7310': 1.33,
}

MS8_JSON = os.path.join(
    ROOT, 'trades/active/MANUAL_SPREAD/MANUAL_SPREAD_SPX_260625_ms8_1324_P_865168.json'
)
MEIC_0200_P_GHOSTS = [
    'MEIC_IC_SPX_260625_0200_1359_P_885114.json',
    'MEIC_IC_SPX_260625_0200_1359_P_885165.json',
    'MEIC_IC_SPX_260625_0200_1359_P_885227.json',
]


def _banner(title: str) -> None:
    print('\n' + '=' * 72)
    print(title)
    print('=' * 72)


def _eval_credit(short_p: float, long_p: float, *, ss: int, ls: int, opt_type: str = 'P') -> dict:
    short_sym = build_tastytrade_symbol(EXPIRY, opt_type, ss)
    long_sym = build_tastytrade_symbol(EXPIRY, opt_type, ls)
    cand = _evaluate_spread(
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=ss,
        long_strike=ls,
        short_p=short_p,
        long_p=long_p,
        opt_type=opt_type,
        target_credit=None,
        credit_min=meic_config.CREDIT_MIN,
        credit_max=meic_config.CREDIT_MAX_P,
        min_market_credit=0.05,
        check_overlap=False,
    )
    raw = short_p - long_p
    rounded = _round_credit(raw) if raw >= 0.05 else None
    return {
        'raw': round(raw, 2),
        'rounded': rounded,
        'in_band': cand is not None,
        'candidate_credit': cand.market_credit if cand else None,
    }


def investigate_01_15_put() -> None:
    _banner('CASE 1 — 01-15 put (overlap shift + scan pick)')

    pairs = [
        (7325, 7300, 'scan hit 1 (overlap with ms-5 short @ 7300)'),
        (7320, 7295, 'scan hit 2 (no overlap)'),
        (7335, 7310, 'overlap shift target (+$5 both legs)'),
    ]
    for ss, ls, label in pairs:
        s = build_tastytrade_symbol(EXPIRY, 'P', ss)
        l = build_tastytrade_symbol(EXPIRY, 'P', ls)
        sp = PRICES_01_15_SCAN.get(s) or PRICES_01_15_SHIFT_EST.get(s)
        lp = PRICES_01_15_SCAN.get(l) or PRICES_01_15_SHIFT_EST.get(l)
        overlap = leg_overlap_conflict(s, l, 'P')
        credit = _eval_credit(sp, lp, ss=ss, ls=ls) if sp is not None and lp is not None else None
        print(f'\n{label}: {ss}/{ls}')
        print(f'  overlap: {overlap or "none"}')
        if sp is not None and lp is not None:
            print(f'  mids: short={sp:.2f} long={lp:.2f}  credit raw={credit["raw"]} rounded={credit["rounded"]} in_band={credit["in_band"]}')
        else:
            print('  mids: (not in replay set)')

    print('\nScan return simulation (max_results=1, two candidates, first has overlap):')
    from blocks.entry.spread_scan import SpreadCandidate

    c1 = SpreadCandidate(
        short_symbol='.SPXW260625P7325', long_symbol='.SPXW260625P7300',
        short_strike=7325, long_strike=7300, market_credit=1.70,
        short_mid=2.55, long_mid=0.82, overlap_warning='long 7300 open as short ms-5',
    )
    c2 = SpreadCandidate(
        short_symbol='.SPXW260625P7320', long_symbol='.SPXW260625P7295',
        short_strike=7320, long_strike=7295, market_credit=1.30,
        short_mid=1.98, long_mid=0.68, overlap_warning=None,
    )
    candidates = [c1, c2]
    picked = None
    for c in candidates:
        if not c.overlap_warning:
            picked = c
            break
    entry_pick = candidates[0]
    print(f'  scan logic would return clean: {picked.short_strike}/{picked.long_strike if picked else None}')
    print(f'  open_spread_tt uses candidates[0]: {entry_pick.short_strike}/{entry_pick.long_strike} overlap={bool(entry_pick.overlap_warning)}')
    print('  => TerminateRequest before any order (matches 01-15_put.log ending after scan lines)')


def investigate_02_00_dashboard() -> None:
    _banner('CASE 2 — 02-00 put breach hidden on dashboard')

    active = os.path.join(ROOT, 'trades/active/MEIC_IC')
    rows = []
    for name in MEIC_0200_P_GHOSTS:
        path = os.path.join(active, name)
        if not os.path.isfile(path):
            rows.append((name, 'MISSING', '', '', ''))
            continue
        st = state_mod.load_state(path)
        rows.append((
            name,
            st.get('status'),
            st.get('open_order_id'),
            st.get('close_mechanism'),
            st.get('entry', {}).get('net_credit'),
        ))

    print('Files for lot 02-00 side P (glob sort order = dashboard matching[0] order):')
    for name, status, oid, mech, credit in sorted(rows, key=lambda r: r[0]):
        print(f'  {name}')
        print(f'    status={status} order={oid} close={mech} credit={credit}')

    matching = sorted([r for r in rows if r[1] != 'MISSING'], key=lambda r: r[0])
    if matching:
        dash = matching[0]
        print(f'\nDashboard picks (matching[0]): {dash[0]}')
        print(f'  Shows as: {"open/working" if dash[1] == "pending_fill" else dash[1]}')
        real = next((r for r in matching if r[0].endswith('885227.json')), None)
        if real:
            print(f'  Actual filled+breached trade: {real[0]} status={real[1]} close={real[3]}')


def _format_order_result(result, raw) -> str:
    lines = [
        f'  success={result.success} status={result.status!r}',
        f'  filled_quantity={result.filled_quantity} order_quantity={result.order_quantity}',
        f'  filled_price(credit)={result.filled_price}',
        f'  short_fill_price={getattr(result, "short_fill_price", None)}',
        f'  long_fill_price={getattr(result, "long_fill_price", None)}',
    ]
    if raw:
        lines.append(f'  raw order_type={getattr(raw, "order_type", None)} price={getattr(raw, "price", None)}')
        legs = getattr(raw, 'legs', None) or []
        for i, leg in enumerate(legs):
            action = getattr(leg, 'action', None)
            sym = getattr(leg, 'symbol', None)
            qty = getattr(leg, 'quantity', None)
            rem = getattr(leg, 'remaining_quantity', None)
            fills = getattr(leg, 'fills', None) or []
            fill_detail = [(int(f.quantity), float(f.fill_price)) for f in fills] if fills else []
            lines.append(f'  leg[{i}] {action} {sym} qty={qty} rem={rem} fills={fill_detail}')
    return '\n'.join(lines)


def investigate_ms8_json_only() -> None:
    _banner('CASE 3 — ms-8 manual 3-lot put (JSON state)')

    if not os.path.isfile(MS8_JSON):
        print(f'Missing {MS8_JSON}')
        return
    st = state_mod.load_state(MS8_JSON)
    filename_oid = '865168'  # from filename tail
    json_oid = st.get('open_order_id')
    print(f'File: {os.path.basename(MS8_JSON)}')
    print(f'  lot={st.get("lot")} qty={st.get("quantity")} status={st.get("status")}')
    print(f'  strikes {st["short_leg"]["strike"]}/{st["long_leg"]["strike"]} limit={st["entry"].get("limit_credit")}')
    print(f'  filename order tail: {filename_oid}  JSON open_order_id: {json_oid}')
    if filename_oid != str(json_oid)[-6:]:
        print('  => open_order_id differs from filename — likely modify_spread replaced order')
    print(f'  filled_quantity={st.get("filled_quantity")} leg fills={st["short_leg"].get("fill_price")}/{st["long_leg"].get("fill_price")}')
    print(f'  open_order.status={st.get("open_order", {}).get("status")} last_sync={st.get("open_order", {}).get("last_sync")}')
    print(f'  active_stop={st.get("active_stop")}')
    print(f'  recovery module_start_count={st.get("recovery", {}).get("module_start_count")}')


def investigate_ms8_broker(order_ids: List[str]) -> None:
    _banner('CASE 3 — ms-8 broker order lookup')

    from common.broker_factory import get_broker
    from brokers.tastytrade_broker import _normalize_leg_action

    broker = get_broker()
    for oid in order_ids:
        print(f'\nOrder {oid}:')
        result = broker.get_order_status(str(oid))
        print(_format_order_result(result, result.raw))

    print('\nSearching live orders for 7335/7310 spread legs (Jun 25 expiry):')
    try:
        live = broker._run(broker.account.get_live_orders(broker.session))
    except Exception as exc:
        print(f'  live orders failed: {exc}')
        live = []

    targets = {'.SPXW260625P7335', '.SPXW260625P7310'}
    for order in live or []:
        leg_syms = {str(getattr(leg, 'symbol', '')) for leg in (getattr(order, 'legs', None) or [])}
        if leg_syms & targets:
            print(f'  LIVE {order.id} status={order.status} type={order.order_type} legs={leg_syms}')

    print('\nfill_sync dry-run on ms-8 JSON using JSON open_order_id:')
    st = state_mod.load_state(MS8_JSON)
    before = copy.deepcopy(st)
    changed, result = fill_sync.sync_open_order(st, broker, force=True, min_interval_sec=0)
    print(f'  changed={changed}')
    if result:
        print(_format_order_result(result, result.raw))
    print(f'  after: status={st.get("status")} filled={st.get("filled_quantity")} '
          f'short={st["short_leg"].get("fill_price")} long={st["long_leg"].get("fill_price")}')
    if st.get('status') == 'open':
        print('  => fill_sync WOULD promote to open (stop_monitor could place stop on next poll)')
    elif before.get('open_order_id') != st.get('open_order_id'):
        print(f'  => open_order_id updated to {st.get("open_order_id")}')


def investigate_02_00_broker() -> None:
    """Optional: confirm ghost orders cancelled, real order filled."""
    from common.broker_factory import get_broker

    broker = get_broker()
    print('\nBroker status for 02-00 put orders:')
    for oid in ('478885114', '478885165', '478885227'):
        r = broker.get_order_status(oid)
        print(f'  {oid}: status={r.status!r} filled={r.filled_quantity}/{r.order_quantity}')


def investigate_01_15_overlap_at_incident() -> None:
    """7325/7300 conflict requires ms-5 short @ 7300 (open at 13:14, closed now)."""
    print('\nNote: 7325/7300 overlap at incident time = ms-5 manual short 7300 (long 7300 on new spread).')
    print('  Current book may not show overlap if ms-5 is closed/archived.')


def main() -> None:
    parser = argparse.ArgumentParser(description='Investigate Jun 25 incidents')
    parser.add_argument(
        '--broker', action='store_true',
        help='Query TastyTrade for ms-8 and related order ids',
    )
    parser.add_argument(
        '--orders', nargs='*', default=['478865557', '478865168'],
        help='Order ids to look up with --broker',
    )
    args = parser.parse_args()

    investigate_01_15_put()
    investigate_01_15_overlap_at_incident()
    investigate_02_00_dashboard()
    investigate_ms8_json_only()
    if args.broker:
        investigate_02_00_broker()
        investigate_ms8_broker(args.orders)
    else:
        print('\n(Tip: run with --broker to query TastyTrade for ms-8 order status)')

    _banner('DONE')


if __name__ == '__main__':
    main()
