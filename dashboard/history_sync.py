"""Sync closed / expired trades from JSON on disk into SQLite history."""
from __future__ import annotations

import json
import logging
import os
from datetime import date
from typing import Any, Dict, List, Optional

from common.expiry_settlement import (
    ensure_spx_settlement_close,
    get_spx_settlement_close,
    has_real_close_fills,
    iter_all_trade_json_paths,
    iter_trade_json_paths,
    trade_to_history_row,
)
from common.session_cleanup import central_today
from dashboard.db import delete_trades_before, refresh_all_daily_summaries, upsert_trade

log = logging.getLogger(__name__)


def _load_trade(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        log.warning('history_sync: could not read %s', path)
    return None


def _valid_history_keys_for_date(
    root: str,
    target: date,
    spx: Optional[float],
) -> set[tuple[str, str]]:
    """(lot, side) pairs that should exist in SQLite for target date."""
    from common.test_trades import is_test_trade

    valid: set[tuple[str, str]] = set()
    for path in iter_trade_json_paths(root, for_date=target):
        trade = _load_trade(path)
        if not trade or is_test_trade(trade, path):
            continue
        row = trade_to_history_row(trade, spx_close=spx)
        if row is None or row.get('date_opened') != target.isoformat():
            continue
        valid.add((row['lot'], row['side']))
    return valid


def purge_stale_history_rows(
    root: str,
    *,
    for_date: Optional[date] = None,
    spx_close: Optional[float] = None,
) -> int:
    """Drop SQLite rows that no longer qualify (ghost ms-205, re-synced test lots, etc.)."""
    target = for_date or central_today()
    spx = spx_close if spx_close is not None else ensure_spx_settlement_close(target, root=root)
    valid = _valid_history_keys_for_date(root, target, spx)
    deleted = 0
    iso = target.isoformat()
    from dashboard.db import get_conn

    with get_conn() as conn:
        rows = conn.execute(
            'SELECT id, lot, side FROM trades WHERE date_opened = ?',
            (iso,),
        ).fetchall()
        for row in rows:
            if (row['lot'], row['side']) not in valid:
                conn.execute('DELETE FROM trades WHERE id = ?', (row['id'],))
                deleted += 1
    if deleted:
        refresh_all_daily_summaries()
        log.info('history_sync: purged %d stale row(s) for %s', deleted, iso)
    return deleted


def sync_history_from_disk(
    root: str,
    *,
    for_date: Optional[date] = None,
    spx_close: Optional[float] = None,
) -> Dict[str, Any]:
    """Upsert all settleable trades for a date. Returns summary counts."""
    from common.test_trades import is_test_trade

    target = for_date or central_today()
    spx = spx_close if spx_close is not None else ensure_spx_settlement_close(target, root=root)

    synced = 0
    skipped = 0
    dates_touched: set[str] = set()

    for path in iter_trade_json_paths(root, for_date=target):
        trade = _load_trade(path)
        if not trade or is_test_trade(trade, path):
            skipped += 1
            continue
        row = trade_to_history_row(trade, spx_close=spx)
        if row is None:
            skipped += 1
            continue
        if row.get('date_opened') != target.isoformat():
            continue
        try:
            upsert_trade(row)
            synced += 1
            dates_touched.add(row['date_opened'])
        except Exception:
            log.exception('history_sync: upsert failed for %s', path)
            skipped += 1

    purged = purge_stale_history_rows(root, for_date=target, spx_close=spx)

    return {
        'date': target.isoformat(),
        'spx_close': spx,
        'synced': synced,
        'skipped': skipped,
        'purged': purged,
        'dates_touched': sorted(dates_touched),
    }


def _trade_priority(trade: Dict[str, Any]) -> int:
    """Prefer stopped/closed JSON over open archives when duplicates exist."""
    if has_real_close_fills(trade):
        return 3
    if (trade.get('status') or '').lower() == 'closed':
        return 2
    return 1


def _trade_key(trade: Dict[str, Any]) -> Optional[tuple[str, str, str]]:
    entry = trade.get('entry') or {}
    ts = entry.get('timestamp') or ''
    if len(ts) < 10:
        return None
    lot = trade.get('lot') or entry.get('lot') or ''
    side = (entry.get('side') or trade.get('side') or '').upper()
    if not lot or not side:
        return None
    return (ts[:10], lot, side)


def _skip_test_trade(trade: Dict[str, Any], path: str) -> bool:
    from common.test_trades import is_test_trade

    return is_test_trade(trade, path)


def sync_all_history_from_disk(
    root: str,
    *,
    otm_backfill_before: Optional[date] = None,
    purge_before: Optional[date] = None,
) -> Dict[str, Any]:
    """Scan all trade JSON on disk and upsert settleable rows into SQLite."""
    if purge_before is not None:
        delete_trades_before(purge_before.isoformat())

    best_by_key: Dict[tuple[str, str, str], tuple[Dict[str, Any], str]] = {}
    skipped = 0

    for path in iter_all_trade_json_paths(root):
        trade = _load_trade(path)
        if not trade or _skip_test_trade(trade, path):
            skipped += 1
            continue
        key = _trade_key(trade)
        if key is None:
            skipped += 1
            continue
        existing = best_by_key.get(key)
        if existing is None or _trade_priority(trade) > _trade_priority(existing[0]):
            best_by_key[key] = (trade, path)

    synced = 0
    dates_touched: set[str] = set()
    pnl_by_date: Dict[str, float] = {}

    for (_d, _lot, _side), (trade, path) in sorted(best_by_key.items()):
        entry = trade.get('entry') or {}
        ts = entry.get('timestamp') or ''
        try:
            entry_date = date.fromisoformat(ts[:10])
        except ValueError:
            skipped += 1
            continue

        if otm_backfill_before and entry_date < otm_backfill_before:
            row = trade_to_history_row(trade, assume_otm_expiry=True)
        else:
            spx = ensure_spx_settlement_close(entry_date, root=root)
            row = trade_to_history_row(trade, spx_close=spx)

        if row is None:
            skipped += 1
            continue
        try:
            upsert_trade(row)
            synced += 1
            d = row['date_opened']
            dates_touched.add(d)
            pnl_by_date[d] = pnl_by_date.get(d, 0.0) + float(row.get('pnl') or 0)
        except Exception:
            log.exception('history_sync: upsert failed for %s', path)
            skipped += 1

    refresh_all_daily_summaries()

    for d in sorted(set(dates_touched) | {central_today().isoformat()}):
        try:
            purge_stale_history_rows(root, for_date=date.fromisoformat(d))
        except ValueError:
            pass

    return {
        'synced': synced,
        'skipped': skipped,
        'dates_touched': sorted(dates_touched),
        'pnl_by_date': {k: round(v, 2) for k, v in sorted(pnl_by_date.items())},
    }
