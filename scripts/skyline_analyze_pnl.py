#!/usr/bin/env python3
"""Compute Skyline intraday max P/L from options_quotes.csv + trade JSON.

Usage:
  python scripts/skyline_analyze_pnl.py --date 2026-07-31
  python scripts/skyline_analyze_pnl.py --trade trades/history/SKYLINE_SPX/skyline_2026-07-31.json
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.skyline.settlement import (  # noqa: E402
    attach_dollar_pnl_display,
    pnl_points_to_dollars,
    update_unrealized_marks,
)


def _load_trade(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


def _parse_quotes(quotes_path: Path) -> dict[str, dict[str, float]]:
    """snapshot_ts -> symbol -> mid"""
    by_ts: dict[str, dict[str, float]] = defaultdict(dict)
    if not quotes_path.is_file():
        return by_ts
    with quotes_path.open(encoding='utf-8', newline='') as f:
        for row in csv.DictReader(f):
            ts = row.get('snapshot_ts', '').strip()
            sym = row.get('symbol', '').strip()
            if not ts or not sym:
                continue
            try:
                by_ts[ts][sym] = float(row['mid'])
            except (TypeError, ValueError):
                continue
    return by_ts


def analyze(trade_path: Path, quotes_path: Path | None) -> None:
    trade = _load_trade(trade_path)
    qty = int(trade.get('quantity') or 1)
    structures = [s for s in trade.get('structures') or [] if s.get('status') not in ('cancelled', 'rejected')]

    print(f'Trade: {trade_path}')
    print(f'Date: {trade.get("date")}  ATM: {trade.get("atm")}  Qty: {qty}')
    print(f'Net debit total: {trade.get("net_debit_total")} pts')
    if trade.get('realized_pnl_total') is not None:
        print(
            f'EOD realized: {trade["realized_pnl_total"]} pts '
            f'({pnl_points_to_dollars(trade["realized_pnl_total"], qty):.2f} USD)'
        )
    print()

    if quotes_path is None:
        day = trade.get('date', '')
        quotes_path = ROOT / 'data' / str(day) / 'options_quotes.csv'
    print(f'Quotes: {quotes_path} (exists={quotes_path.is_file()})')
    by_ts = _parse_quotes(quotes_path)
    if not by_ts:
        print('No option quote snapshots — cannot compute MTM max profit.')
        return

    needed = {leg['symbol'] for s in structures for leg in s.get('legs') or [] if leg.get('symbol')}
    max_total = None
    max_ts = None
    rows_used = 0

    for ts in sorted(by_ts):
        mids = by_ts[ts]
        if not needed.issubset(mids.keys()):
            continue
        scratch = json.loads(json.dumps(trade))
        update_unrealized_marks(scratch, mids)
        attach_dollar_pnl_display(scratch)
        total = scratch.get('unrealized_pnl_dollars')
        if total is None:
            continue
        rows_used += 1
        if max_total is None or total > max_total:
            max_total, max_ts = total, ts

    if max_total is None:
        print(f'No complete snapshots covering all {len(needed)} leg symbols.')
        return

    print(f'Complete snapshots: {rows_used}')
    print(f'Max MTM P/L: ${max_total:.2f} at {max_ts} CT')


def main() -> None:
    parser = argparse.ArgumentParser(description='Skyline max P/L from options_quotes.csv')
    parser.add_argument('--date', help='YYYY-MM-DD (default: from trade file)')
    parser.add_argument('--trade', help='Path to skyline_*.json trade file')
    parser.add_argument('--quotes', help='Path to options_quotes.csv (optional)')
    args = parser.parse_args()

    if args.trade:
        trade_path = Path(args.trade)
    elif args.date:
        for candidate in (
            ROOT / 'trades' / 'history' / 'SKYLINE_SPX' / f'skyline_{args.date}.json',
            ROOT / 'trades' / 'active' / 'SKYLINE_SPX' / f'skyline_{args.date}.json',
        ):
            if candidate.is_file():
                trade_path = candidate
                break
        else:
            raise SystemExit(f'No trade file for {args.date}')
    else:
        raise SystemExit('Provide --date or --trade')

    quotes_path = Path(args.quotes) if args.quotes else None
    analyze(trade_path, quotes_path)


if __name__ == '__main__':
    main()
