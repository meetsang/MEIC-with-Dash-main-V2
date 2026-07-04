#!/usr/bin/env python3
"""
Mark manually closed spreads and move them out of trades/active/.

stop_monitor only watches trades/active/*.json on startup — archived files are ignored.

Usage:
    .venv\\Scripts\\python.exe scripts\\mark_manual_close.py --side P --short-strike 7440 --long-strike 7415 --reason profit_taking
    .venv\\Scripts\\python.exe scripts\\mark_manual_close.py --batch
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod

# Default batch: spreads closed manually on 2026-06-22 (edit as needed).
DEFAULT_BATCH = [
    {'side': 'P', 'short_strike': 7440, 'long_strike': 7415, 'reason': 'profit_taking'},
    {'side': 'C', 'short_strike': 7495, 'long_strike': 7520, 'reason': 'manual_stop'},
    {'side': 'C', 'short_strike': 7490, 'long_strike': 7515, 'reason': 'manual_stop'},
]


def _close_one(side: str, short_strike: int, long_strike: int, reason: str) -> int:
    path = state_mod.find_active_by_strikes(side, short_strike, long_strike)
    if not path:
        print(f'  SKIP: no active JSON for {side} {short_strike}/{long_strike}')
        return 1
    closed_path = state_mod.mark_manual_close(path, close_mechanism=reason, reason=reason)
    print(f'  OK: {side} {short_strike}/{long_strike}')
    print(f'      removed: {path}')
    print(f'      archived: {closed_path}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Archive manually closed spreads')
    parser.add_argument('--side', choices=['P', 'C'])
    parser.add_argument('--short-strike', type=int)
    parser.add_argument('--long-strike', type=int)
    parser.add_argument('--reason', default='manual_close')
    parser.add_argument(
        '--batch',
        action='store_true',
        help='Close default batch (PCS 7440/7415, CCS 7495/7520, CCS 7490/7515)',
    )
    args = parser.parse_args()

    if args.batch:
        print('Archiving manual closes (batch):')
        rc = 0
        for item in DEFAULT_BATCH:
            if _close_one(item['side'], item['short_strike'], item['long_strike'], item['reason']):
                rc = 1
        remaining = [
            f for f in os.listdir(state_mod.active_dir()) if f.endswith('.json')
        ]
        print(f'\nRemaining in active/: {len(remaining)}')
        for name in sorted(remaining):
            print(f'  {name}')
        return rc

    if not args.side or args.short_strike is None or args.long_strike is None:
        parser.error('Provide --side --short-strike --long-strike, or use --batch')
    return _close_one(args.side, args.short_strike, args.long_strike, args.reason)


if __name__ == '__main__':
    raise SystemExit(main())
