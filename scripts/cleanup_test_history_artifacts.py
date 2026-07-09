"""Move test fixture JSON out of production history and fix SQLite totals."""
from __future__ import annotations

import os
import shutil

from common import trades_layout
from common.test_trades import is_test_trade
from dashboard.db import delete_trades_by_lots, purge_known_test_trades_from_db, refresh_all_daily_summaries
from dashboard.history_sync import sync_all_history_from_disk

ROOT = trades_layout.project_root()
PROD_HIST = os.path.join(ROOT, trades_layout.MANUAL_HISTORY)
TEST_HIST = os.path.join(ROOT, trades_layout.TEST_MANUAL_HISTORY)


def main() -> None:
    os.makedirs(TEST_HIST, exist_ok=True)
    moved = 0
    for name in sorted(os.listdir(PROD_HIST)):
        if not name.endswith('.json'):
            continue
        src = os.path.join(PROD_HIST, name)
        if not os.path.isfile(src):
            continue
        import json

        with open(src, encoding='utf-8') as f:
            trade = json.load(f)
        if not is_test_trade(trade, src):
            continue
        dst = os.path.join(TEST_HIST, name)
        shutil.move(src, dst)
        moved += 1
        print(f'moved {name} -> trades/test/history/MANUAL_SPREAD/')

    purged = purge_known_test_trades_from_db()
    print(f'purged {purged} known test rows from SQLite')

    extra = delete_trades_by_lots(('ms-205', 'ms-1', 'test-lot'))
    print(f'deleted {extra} ghost/unfilled rows (ms-205 cancelled, ms-1/test-lot fixtures)')

    result = sync_all_history_from_disk(ROOT)
    print(f're-synced {result["synced"]} trades, skipped {result["skipped"]}')
    print(f'today pnl from sync: {result.get("pnl_by_date", {}).get("2026-07-08")}')


if __name__ == '__main__':
    main()
