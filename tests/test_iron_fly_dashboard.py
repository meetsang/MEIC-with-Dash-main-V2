"""Unit tests for Iron Fly dashboard endpoints and PnL helpers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from dashboard.server import app, _iron_fly_today_pnl, build_summary
from common import trades_layout


class TestIronFlyDashboard(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_iron_fly_today_endpoint_empty(self):
        resp = self.client.get('/api/iron_fly/today')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get('strategy'), trades_layout.STRATEGY_IRON_FLY)
        self.assertIn('paper', data)
        self.assertIn('date', data)
        self.assertIn('session', data)
        self.assertIn('history', data)

    def test_iron_fly_today_pnl_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('dashboard.server.ROOT', tmp):
                # When no file exists, pnl is 0.0
                self.assertEqual(_iron_fly_today_pnl(), 0.0)

                # Create fake today book in active dir
                from common.session_cleanup import central_today
                today = central_today().isoformat()
                active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY, tmp)
                os.makedirs(active, exist_ok=True)
                book_path = os.path.join(active, f'iron_fly_{today}.json')
                with open(book_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'date': today,
                        'realized_pnl': 150.0,
                        'open_pnl': -35.0,
                    }, f)

                self.assertEqual(_iron_fly_today_pnl(), 115.0)

    def test_build_summary_contains_iron_fly_pnl(self):
        with patch('dashboard.server._iron_fly_today_pnl', return_value=125.50):
            summary = build_summary()
            self.assertIn('iron_fly_pnl', summary)
            self.assertEqual(summary['iron_fly_pnl'], 125.50)

    def test_iron_fly_close_all_endpoint(self):
        with patch('common.manual_close.close_all_structures', return_value=[]):
            resp = self.client.post('/api/iron_fly/close_all')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('status'), 'ok')
            self.assertEqual(data.get('results'), [])
