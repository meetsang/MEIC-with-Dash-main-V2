"""Stale history row purge removes ghosts like ms-205 from SQLite."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from unittest.mock import patch

from common import trades_layout
from dashboard import db as dashboard_db
from dashboard.history_sync import purge_stale_history_rows, sync_history_from_disk


class TestPurgeStaleHistoryRows(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self._tmp.close()
        self._conn = sqlite3.connect(self._tmp.name, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(dashboard_db.SCHEMA)

        @contextmanager
        def _fake_get_conn():
            yield self._conn

        self._patch = patch.object(dashboard_db, 'get_conn', _fake_get_conn)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._conn.close()
        os.unlink(self._tmp.name)

    def test_purge_removes_cancelled_zero_fill_ghost(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = os.path.join(tmp, trades_layout.MANUAL_HISTORY)
            os.makedirs(hist)
            today = date.today().isoformat()
            ghost = {
                'status': 'cancelled',
                'lot': 'ms-205',
                'quantity': 5,
                'filled_quantity': 0,
                'spread_type': 'credit',
                'entry': {
                    'strategy': 'MANUAL_SPREAD',
                    'lot': 'ms-205',
                    'side': 'C',
                    'timestamp': f'{today}T14:30:38-05:00',
                    'net_credit': 0.3,
                },
                'short_leg': {'symbol': '.SPXW260708C7500', 'strike': 7500, 'fill_price': 0.0},
                'long_leg': {'symbol': '.SPXW260708C7525', 'strike': 7525, 'fill_price': 0.0},
            }
            with open(os.path.join(hist, 'ms-205_C.json'), 'w', encoding='utf-8') as f:
                json.dump(ghost, f)

            with dashboard_db.get_conn() as conn:
                conn.execute(
                    """INSERT INTO trades
                       (date_opened, lot, side, strategy, quantity, open_credit, pnl, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (today, 'ms-205', 'C', 'MANUAL_SPREAD', 5, 0.3, 150.0, 'CLOSED'),
                )
            with dashboard_db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        'SELECT COUNT(*) FROM trades WHERE date_opened = ? AND lot = ?',
                        (today, 'ms-205'),
                    ).fetchone()[0],
                    1,
                )

            deleted = purge_stale_history_rows(tmp, for_date=date.today())
            self.assertEqual(deleted, 1)
            with dashboard_db.get_conn() as conn:
                self.assertEqual(
                    conn.execute(
                        'SELECT COUNT(*) FROM trades WHERE date_opened = ? AND lot = ?',
                        (today, 'ms-205'),
                    ).fetchone()[0],
                    0,
                )

    def test_sync_history_purges_after_upsert(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = os.path.join(tmp, trades_layout.MANUAL_HISTORY)
            os.makedirs(hist)
            today = date.today().isoformat()
            real = {
                'status': 'closed',
                'lot': 'ms-190',
                'filled_quantity': 3,
                'spread_type': 'credit',
                'entry': {
                    'strategy': 'MANUAL_SPREAD',
                    'lot': 'ms-190',
                    'side': 'P',
                    'timestamp': f'{today}T13:04:47-05:00',
                    'net_credit': 0.55,
                },
                'short_leg': {'symbol': '.SPXW260708P7425', 'strike': 7425, 'fill_price': 1.0},
                'long_leg': {'symbol': '.SPXW260708P7400', 'strike': 7400, 'fill_price': 0.45},
                'short_close_price': 0.1,
                'long_close_price': 0.05,
            }
            with open(os.path.join(hist, 'ms-190_P.json'), 'w', encoding='utf-8') as f:
                json.dump(real, f)

            with dashboard_db.get_conn() as conn:
                conn.execute(
                    """INSERT INTO trades
                       (date_opened, lot, side, strategy, quantity, open_credit, pnl, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (today, 'ms-205', 'C', 'MANUAL_SPREAD', 5, 0.3, 150.0, 'CLOSED'),
                )

            result = sync_history_from_disk(tmp, for_date=date.today(), spx_close=7481.46)
            self.assertGreaterEqual(result.get('purged', 0), 1)
            with dashboard_db.get_conn() as conn:
                lots = {
                    r[0]
                    for r in conn.execute(
                        'SELECT lot FROM trades WHERE date_opened = ?',
                        (today,),
                    ).fetchall()
                }
            self.assertIn('ms-190', lots)
            self.assertNotIn('ms-205', lots)


if __name__ == '__main__':
    unittest.main()
