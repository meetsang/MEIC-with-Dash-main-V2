"""Tests for 0DTE expiry settlement."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, datetime

from common.expiry_settlement import (
    capture_mqtt_settlement_close,
    compute_settled_pnl,
    ensure_spx_settlement_close,
    get_spx_settlement_close,
    resolve_spx_settlement_close,
    settlement_cutoff_reached,
    spread_intrinsic_at_expiry,
    trade_to_history_row,
    write_spx_settlement_close,
)


class TestExpirySettlement(unittest.TestCase):
    def test_put_credit_otm_full_profit(self):
        intrinsic = spread_intrinsic_at_expiry('P', 7455, 7430, 7499.0)
        self.assertEqual(intrinsic, 0.0)
        trade = {
            'spread_type': 'credit',
            'status': 'open',
            'filled_quantity': 1,
            'entry': {'side': 'P', 'net_credit': 1.4, 'timestamp': '2026-06-30T10:59:03-05:00'},
            'short_leg': {'symbol': '.SPXW260630P7455', 'strike': 7455},
            'long_leg': {'symbol': '.SPXW260630P7430', 'strike': 7430},
        }
        now = datetime(2026, 6, 30, 15, 30)
        self.assertTrue(settlement_cutoff_reached(date(2026, 6, 30), now=now))
        settled = compute_settled_pnl(trade, 7499.0)
        self.assertIsNotNone(settled)
        self.assertTrue(settled['settled'])
        self.assertEqual(settled['pnl'], 140.0)

    def test_call_credit_stopped_uses_fills(self):
        trade = {
            'spread_type': 'credit',
            'status': 'closed',
            'filled_quantity': 1,
            'entry': {'side': 'C', 'net_credit': 0.9, 'timestamp': '2026-06-30T13:14:08-05:00'},
            'short_leg': {'strike': 7515},
            'long_leg': {'strike': 7540},
            'short_close_price': 1.95,
            'long_close_price': 0.1,
        }
        settled = compute_settled_pnl(trade, 7499.0)
        self.assertEqual(settled['pnl'], -95.0)

    def test_meic_day_pnl_components(self):
        """Sanity: OTM open legs + stopped calls = +335 for Jun 30 fixture."""
        rows = [
            ('P', 1.4, 7455, 7430, 'open'),
            ('P', 1.1, 7465, 7440, 'open'),
            ('P', 1.1, 7465, 7440, 'open'),
            ('P', 1.1, 7475, 7450, 'open'),
            ('P', 1.5, 7485, 7460, 'open'),
            ('P', 1.05, 7485, 7460, 'open'),
            ('C', 1.0, 7520, 7545, 'open'),
            ('C', 0.9, 7515, 7540, 'closed', 1.95, 0.1),
            ('C', 0.85, 7515, 7540, 'closed', 1.55, 0.0),
            ('C', 1.05, 7515, 7540, 'closed', 2.0, 0.0),
            ('C', 1.1, 7515, 7540, 'closed', 2.3, 0.0),
            ('C', 1.15, 7510, 7535, 'closed', 2.65, 0.4),
        ]
        total = 0.0
        for row in rows:
            trade = {
                'spread_type': 'credit',
                'status': row[4],
                'filled_quantity': 1,
                'entry': {'side': row[0], 'net_credit': row[1], 'timestamp': '2026-06-30T10:00:00-05:00'},
                'short_leg': {'symbol': f'.SPXW260630{row[0]}{row[2]}', 'strike': row[2]},
                'long_leg': {'symbol': f'.SPXW260630{row[0]}{row[3]}', 'strike': row[3]},
            }
            if len(row) > 5:
                trade['short_close_price'] = row[5]
                trade['long_close_price'] = row[6]
            settled = compute_settled_pnl(trade, 7499.0, now=datetime(2026, 6, 30, 15, 30))
            total += settled['pnl']
        self.assertAlmostEqual(total, 335.0, places=1)

    def test_otm_decay_without_spx(self):
        trade = {
            'lot': 'ms-10',
            'spread_type': 'credit',
            'status': 'open',
            'filled_quantity': 1,
            'entry': {
                'side': 'P',
                'net_credit': 1.0,
                'timestamp': '2026-06-26T06:55:59-05:00',
                'lot': 'ms-10',
            },
            'short_leg': {'symbol': '.SPXW260626P6700', 'strike': 6700},
            'long_leg': {'symbol': '.SPXW260626P6675', 'strike': 6675},
        }
        row = trade_to_history_row(trade, assume_otm_expiry=True)
        self.assertEqual(row['pnl'], 100.0)
        self.assertTrue(row['settled_at_expiry'])

    def test_trade_to_history_row(self):
        trade = {
            'lot': '11-00',
            'spread_type': 'credit',
            'status': 'open',
            'filled_quantity': 1,
            'entry': {
                'side': 'P',
                'net_credit': 1.4,
                'timestamp': '2026-06-30T10:59:03-05:00',
                'lot': '11-00',
            },
            'short_leg': {'symbol': '.SPXW260630P7455', 'strike': 7455, 'fill_price': 2.42},
            'long_leg': {'symbol': '.SPXW260630P7430', 'strike': 7430, 'fill_price': 1.02},
        }
        row = trade_to_history_row(trade, spx_close=7499.0)
        self.assertEqual(row['pnl'], 140.0)
        self.assertEqual(row['status'], 'CLOSED')

    def test_stale_manual_file_loses_to_spx_polls(self):
        """Unlocked manual settlement must not beat session polls before MQTT cutoff."""
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 7, 7)
            settlement_dir = os.path.join(tmp, 'trades', 'settlement')
            data_dir = os.path.join(tmp, 'data', day.isoformat())
            os.makedirs(settlement_dir)
            os.makedirs(data_dir)
            with open(os.path.join(settlement_dir, f'{day.isoformat()}.json'), 'w', encoding='utf-8') as f:
                json.dump({'date': day.isoformat(), 'spx_close': 7524.29, 'source': 'manual'}, f)
            with open(os.path.join(data_dir, 'SPX_polls.csv'), 'w', encoding='utf-8') as f:
                f.write('timestamp,price\n')
                f.write('2026-07-07 14:59:49,7500.15\n')

            before_cutoff = datetime(2026, 7, 7, 14, 30)
            resolved = resolve_spx_settlement_close(day, root=tmp, now=before_cutoff)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved['source'], 'spx_polls')
            self.assertEqual(get_spx_settlement_close(day, root=tmp, now=before_cutoff), 7500.15)

    def test_mqtt_settlement_wins_after_3pm(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 7, 7)
            data_dir = os.path.join(tmp, 'data', day.isoformat())
            os.makedirs(data_dir)
            with open(os.path.join(data_dir, 'SPX_polls.csv'), 'w', encoding='utf-8') as f:
                f.write('timestamp,price\n')
                f.write('2026-07-07 14:59:49,7500.15\n')
            with open(os.path.join(data_dir, 'spx_mqtt_settlement.json'), 'w', encoding='utf-8') as f:
                json.dump({
                    'date': day.isoformat(),
                    'spx_close': 7503.0,
                    'source': 'mqtt_settlement',
                    'captured_at': '2026-07-07T15:00:05-05:00',
                }, f)

            after_cutoff = datetime(2026, 7, 7, 15, 5)
            self.assertEqual(get_spx_settlement_close(day, root=tmp, now=after_cutoff), 7503.0)

    def test_capture_mqtt_settlement_persists_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 7, 7)
            os.makedirs(os.path.join(tmp, 'data', day.isoformat()))
            after_cutoff = datetime(2026, 7, 7, 15, 0, 5)

            class _FakeCache:
                def get_spx(self):
                    return 7503.205

            import common.expiry_settlement as mod
            original = mod._spx_from_mqtt_snapshot
            mod._spx_from_mqtt_snapshot = lambda: _FakeCache().get_spx()
            try:
                first = capture_mqtt_settlement_close(day, root=tmp, now=after_cutoff)
                second = capture_mqtt_settlement_close(day, root=tmp, now=after_cutoff)
            finally:
                mod._spx_from_mqtt_snapshot = original

            self.assertEqual(first, 7503.205)
            self.assertEqual(second, 7503.205)
            self.assertEqual(get_spx_settlement_close(day, root=tmp, now=after_cutoff), 7503.205)

    def test_locked_manual_file_wins_over_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 7, 7)
            data_dir = os.path.join(tmp, 'data', day.isoformat())
            os.makedirs(data_dir)
            write_spx_settlement_close(day, 7503.0, root=tmp, source='operator_manual', locked=True)
            with open(os.path.join(data_dir, 'SPX_polls.csv'), 'w', encoding='utf-8') as f:
                f.write('timestamp,price\n')
                f.write('2026-07-07 14:59:49,7500.15\n')

            self.assertEqual(get_spx_settlement_close(day, root=tmp), 7503.0)

    def test_ensure_rewrites_stale_manual_from_polls(self):
        with tempfile.TemporaryDirectory() as tmp:
            day = date(2026, 7, 7)
            settlement_dir = os.path.join(tmp, 'trades', 'settlement')
            data_dir = os.path.join(tmp, 'data', day.isoformat())
            os.makedirs(settlement_dir)
            os.makedirs(data_dir)
            with open(os.path.join(settlement_dir, f'{day.isoformat()}.json'), 'w', encoding='utf-8') as f:
                json.dump({'date': day.isoformat(), 'spx_close': 7524.29, 'source': 'manual'}, f)
            with open(os.path.join(data_dir, 'SPX_polls.csv'), 'w', encoding='utf-8') as f:
                f.write('timestamp,price\n')
                f.write('2026-07-07 14:59:49,7500.15\n')

            before_cutoff = datetime(2026, 7, 7, 14, 45)
            spx = ensure_spx_settlement_close(day, root=tmp, now=before_cutoff)
            self.assertEqual(spx, 7500.15)
            with open(os.path.join(settlement_dir, f'{day.isoformat()}.json'), encoding='utf-8') as f:
                saved = json.load(f)
            self.assertEqual(saved['spx_close'], 7500.15)
            self.assertEqual(saved['source'], 'spx_polls')
            self.assertFalse(saved['locked'])


if __name__ == '__main__':
    unittest.main()
