"""Unit tests for XSP Iron Fly dashboard endpoints and PnL helpers."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

from common import trades_layout
from dashboard.server import _iron_fly_xsp_today_pnl, app, build_summary


class TestIronFlyXspDashboard(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_iron_fly_xsp_today_endpoint_empty(self):
        resp = self.client.get('/api/iron_fly_xsp/today')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get('strategy'), trades_layout.STRATEGY_IRON_FLY_XSP)
        self.assertIn('paper', data)
        self.assertIn('date', data)
        self.assertIn('session', data)
        self.assertIn('history', data)

    def test_iron_fly_xsp_today_pnl_helper(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch('dashboard.server.ROOT', tmp):
                # When no file exists, pnl is 0.0
                self.assertEqual(_iron_fly_xsp_today_pnl(), 0.0)

                # Create fake today book in active dir
                from common.session_cleanup import central_today
                today = central_today().isoformat()
                active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, tmp)
                os.makedirs(active, exist_ok=True)
                book_path = os.path.join(active, f'iron_fly_xsp_{today}.json')
                with open(book_path, 'w', encoding='utf-8') as f:
                    json.dump({
                        'date': today,
                        'realized_pnl': 85.0,
                        'open_pnl': -12.5,
                    }, f)

                self.assertEqual(_iron_fly_xsp_today_pnl(), 72.5)

    def test_build_summary_contains_iron_fly_xsp_pnl(self):
        with patch('dashboard.server._iron_fly_xsp_today_pnl', return_value=95.00):
            summary = build_summary()
            self.assertIn('iron_fly_xsp_pnl', summary)
            self.assertEqual(summary['iron_fly_xsp_pnl'], 95.00)

    def test_iron_fly_xsp_close_all_endpoint(self):
        with patch('common.manual_close.close_all_structures', return_value=[]):
            resp = self.client.post('/api/iron_fly_xsp/close_all')
            self.assertEqual(resp.status_code, 200)
            data = resp.get_json()
            self.assertEqual(data.get('status'), 'ok')
            self.assertEqual(data.get('results'), [])
