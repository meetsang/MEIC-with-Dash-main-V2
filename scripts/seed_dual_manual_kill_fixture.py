#!/usr/bin/env python3
"""Seed open manual-spread JSON fixtures for dual kill / close testing.

Models two live CCS positions (different expiries) from operator screenshot:
  - Jul 6 7600/7625  stop #480934535
  - Jul 7 7585/7610  stop #480934537

Default: writes to trades/sandbox/dual_kill/ (safe — stop_monitor does not watch there).
Use --apply to write under trades/active/MANUAL_SPREAD/ (stop_monitor WILL pick them up).

Use --write-kill-commands to drop trades/commands/*.close.json (dashboard-style kill).

Example (safe simulation tree):
  python scripts/seed_dual_manual_kill_fixture.py

Example (live active dir — only if you intend stop_monitor to manage these JSONs):
  python scripts/seed_dual_manual_kill_fixture.py --apply --lots ms-99 ms-100
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod
from common.symbols import build_tastytrade_symbol
from common import trades_layout

# Operator positions (2026-07-04 screenshot) — qty 3 each.
FIXTURES: Tuple[Dict[str, Any], ...] = (
    {
        'label': 'Jul6 7600/7625 CCS',
        'lot': 'ms-99',
        'expiry_yymmdd': '260706',
        'short_strike': 7600,
        'long_strike': 7625,
        'short_fill': 0.82,
        'long_fill': 0.27,
        'net_credit': 0.55,
        'quantity': 3,
        'open_order_id': '480934100',
        'stop_order_id': '480934535',
        'stop_price': 1.60,
        'limit_price': 1.70,
        # Approx mids for spread-close pricing if MQTT absent in test
        'short_mid': 0.22,
        'long_mid': 0.10,
    },
    {
        'label': 'Jul7 7585/7610 CCS',
        'lot': 'ms-100',
        'expiry_yymmdd': '260707',
        'short_strike': 7585,
        'long_strike': 7610,
        'short_fill': 0.80,
        'long_fill': 0.30,
        'net_credit': 0.50,
        'quantity': 3,
        'open_order_id': '480934150',
        'stop_order_id': '480934537',
        'stop_price': 1.60,
        'limit_price': 1.70,
        'short_mid': 1.82,
        'long_mid': 0.60,
    },
)


def _build_state(spec: Dict[str, Any], *, lot: str) -> Dict[str, Any]:
    short_sym = build_tastytrade_symbol(spec['expiry_yymmdd'], 'C', spec['short_strike'])
    long_sym = build_tastytrade_symbol(spec['expiry_yymmdd'], 'C', spec['long_strike'])
    st = state_mod.create_new_state(
        strategy=trades_layout.STRATEGY_MANUAL,
        lot=lot,
        side='C',
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=spec['short_strike'],
        long_strike=spec['long_strike'],
        short_fill=spec['short_fill'],
        long_fill=spec['long_fill'],
        net_credit=spec['net_credit'],
        quantity=spec['quantity'],
        open_order_id=spec['open_order_id'],
    )
    qty = spec['quantity']
    st['stop_multiplier'] = 2
    st['on_unfilled'] = 'none'
    st['plan'] = {
        'stop_multiplier': 2,
        'on_unfilled': 'none',
        'fill_wait_sec': 5,
        'chase_floor': 0.0,
        'chase_max_attempts': 0,
        'max_attempts': 1,
        'strategy': trades_layout.STRATEGY_MANUAL,
    }
    st['active_stop'] = {
        'order_id': spec['stop_order_id'],
        'type': 'STOP_LIMIT',
        'stop_price': spec['stop_price'],
        'limit_price': spec['limit_price'],
        'phase': 1,
        'status': 'working',
        'placed_at': state_mod.now_iso(),
        'quantity': qty,
    }
    st['stop_quantity'] = qty
    st['designated_stop_price'] = spec['stop_price']
    st['stop_history'] = [{
        'action': 'placed',
        'order_id': spec['stop_order_id'],
        'price': spec['stop_price'],
        'phase': 1,
        'reason': 'initial_short_stop_2x',
        'timestamp': state_mod.now_iso(),
        'spx_price_at_event': 7483.24,
    }]
    return st


def _filename(lot: str) -> str:
    ts = datetime.now().strftime('%Y%m%dT%H%M%S')
    return f'{lot}_C_{ts}.json'


def main() -> int:
    parser = argparse.ArgumentParser(description='Seed dual manual CCS kill-test fixtures')
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Write to trades/active/MANUAL_SPREAD/ (stop_monitor watches this)',
    )
    parser.add_argument(
        '--write-kill-commands',
        action='store_true',
        help='Also write trades/commands/{file}.close.json for each fixture',
    )
    parser.add_argument(
        '--lots',
        nargs=2,
        metavar='LOT',
        default=['ms-99', 'ms-100'],
        help='Lot ids for the two fixtures (default: ms-99 ms-100)',
    )
    args = parser.parse_args()

    if args.apply:
        active_dir = os.path.join(ROOT, trades_layout.MANUAL_ACTIVE)
        print('WARNING: --apply writes to stop_monitor active dir:', active_dir)
    else:
        active_dir = os.path.join(ROOT, 'trades', 'sandbox', 'dual_kill', 'active', 'MANUAL_SPREAD')

    os.makedirs(active_dir, exist_ok=True)
    cmd_dir = os.path.join(ROOT, trades_layout.commands_dir())
    os.makedirs(cmd_dir, exist_ok=True)

    written: List[str] = []
    for spec, lot in zip(FIXTURES, args.lots):
        state = _build_state(spec, lot=lot)
        fname = _filename(lot)
        path = os.path.join(active_dir, fname)
        state_mod.save_state(path, state)
        written.append(path)
        print(f'Wrote {spec["label"]} -> {path}')
        print(f'  stop {spec["stop_order_id"]}  symbols {state["short_leg"]["symbol"]} / {state["long_leg"]["symbol"]}')

        if args.write_kill_commands:
            cmd_path = os.path.join(cmd_dir, f'{fname}.close.json')
            with open(cmd_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'close_mechanism': 'manual_close',
                    'ts': state_mod.now_iso(),
                    'source': 'seed_dual_manual_kill_fixture',
                }, f, indent=2)
            print(f'  kill command -> {cmd_path}')

    print()
    if not args.apply:
        print('Sandbox mode. Run simulation:')
        print('  python -m pytest tests/test_dual_manual_kill_simulation.py -v')
        print()
        print('To exercise real stop_monitor + dashboard on these JSONs:')
        print('  python scripts/seed_dual_manual_kill_fixture.py --apply --write-kill-commands')
        print('  (Ensure JSONs match YOUR broker legs before --apply on a live account.)')
    else:
        print('Active JSONs written. Restart or wait for MonitorRunner scan.')
        print('Use dashboard Kill Selected or --write-kill-commands on next seed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
