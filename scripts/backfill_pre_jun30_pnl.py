"""Re-sync history PnL for dates before Jun 30 using OTM expiry decay."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from datetime import date

from dashboard.db import get_stats_by_strategy, init_db
from dashboard.history_sync import sync_all_history_from_disk

CUTOFF = date(2026, 6, 30)


def main() -> None:
    init_db()
    result = sync_all_history_from_disk(
        ROOT,
        otm_backfill_before=CUTOFF,
        purge_before=CUTOFF,
    )
    print('Backfill complete:')
    print(f"  synced={result['synced']} skipped={result['skipped']}")
    print('  PnL by date (from this run):')
    for d, pnl in result.get('pnl_by_date', {}).items():
        if d < CUTOFF.isoformat():
            print(f'    {d}: ${pnl:,.2f}')
    print()
    print('Strategy totals (all dates in DB):')
    for strategy, stats in get_stats_by_strategy().items():
        print(
            f"  {strategy}: ${stats.get('total_pnl', 0):,.2f} "
            f"({stats.get('total_trades', 0)} trades)"
        )


if __name__ == '__main__':
    main()
