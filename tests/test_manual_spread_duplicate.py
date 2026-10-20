"""Unit tests for Manual Spread Duplicate / Average feature."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from flask import Flask

from dashboard.manual_spread_handlers import register_manual_spread_routes, build_manual_trades
from manual_spread import entry as ms_entry


class TestManualSpreadDuplicate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.trades_dir = os.path.join(self.root, "trades")
        self.active_dir = os.path.join(self.trades_dir, "active", "MANUAL_SPREAD")
        os.makedirs(self.active_dir, exist_ok=True)

        # Create a sample trade JSON: ms-187 P 7535/7510 @ 0.55
        self.trade_data = {
            "strategy_version": "2.0",
            "instrument": "SPX",
            "spread_type": "credit",
            "stop_profile": "manual_credit_spread",
            "status": "open",
            "lot": "ms-187",
            "quantity": 3,
            "filled_quantity": 3,
            "stop_quantity": 3,
            "open_order_id": "510321970",
            "entry": {
                "strategy": "MANUAL_SPREAD",
                "lot": "ms-187",
                "side": "P",
                "timestamp": "2026-09-29T12:29:45-05:00",
                "net_credit": 0.55,
                "limit_credit": 0.55,
                "fill_credit": 0.55,
            },
            "short_leg": {
                "symbol": ".SPXW260930P7535",
                "strike": 7535,
                "fill_price": 1.53,
            },
            "long_leg": {
                "symbol": ".SPXW260930P7510",
                "strike": 7510,
                "fill_price": 0.98,
            },
            "active_stop": {
                "order_id": "510321980",
                "type": "STOP_LIMIT",
                "stop_price": 7.1,
                "status": "working",
            },
            "stop_multiplier": 5.0,
        }
        self.trade_filename = "ms-187_P_20260929T122944.json"
        self.trade_filepath = os.path.join(self.active_dir, self.trade_filename)
        with open(self.trade_filepath, "w", encoding="utf-8") as f:
            json.dump(self.trade_data, f)

        # Set up Flask test client
        self.app = Flask(__name__)
        register_manual_spread_routes(
            self.app,
            live_prices={},
            index_topic="TASTYTRADE/SPX",
            trades_dir=self.trades_dir,
            central_now_str=lambda: "14:00:00 CT",
            tranche_lots=[],
            tranche_sides=[],
            launcher_active_fn=lambda: True,
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_find_trade_by_filename(self):
        with patch("blocks.stop.state.manual_spread_active_dir", return_value=self.active_dir):
            found = ms_entry.find_trade_by_filename(self.trade_filename)
            self.assertIsNotNone(found)
            self.assertEqual(found.get("lot"), "ms-187")
            self.assertEqual(found.get("short_leg", {}).get("strike"), 7535)

    def test_duplicate_manual_spread_endpoint_success(self):
        """Verify duplicating a 55c trade at 1.20 creates a new session row with identical strikes and new limit."""
        with patch("blocks.stop.state.manual_spread_active_dir", return_value=self.active_dir):
            with patch("blocks.session.manual_helpers.is_after_market_close_ct", return_value=False):
                with patch("common.trading_gate.effective_new_risk_blocked", return_value=False):
                    res = self.client.post(
                        "/api/manual_spread/duplicate",
                        json={
                            "filename": self.trade_filename,
                            "limit_credit": 1.20,
                            "quantity": 3,
                            "stop_multiplier": 3.0,
                        },
                    )
                    self.assertEqual(res.status_code, 200)
                    data = res.get_json()
                    self.assertEqual(data.get("status"), "entering")
                    self.assertIn("lot", data)
                    self.assertIn("slot_key", data)

                    # Verify session plan row was written
                    from blocks.session.plan import SessionPlan

                    plan = SessionPlan.load(data["session_path"])
                    row = plan.row_by_slot_key(data["slot_key"])
                    self.assertIsNotNone(row)
                    self.assertEqual(row.side, "P")
                    self.assertEqual(row.short_strike, 7535)
                    self.assertEqual(row.long_strike, 7510)
                    self.assertEqual(row.limit_credit, 1.20)
                    self.assertEqual(row.quantity, 3)
                    self.assertEqual(row.stop_multiplier, 3.0)
                    self.assertEqual(row.expiry, "260930")

    def test_duplicate_missing_filename_returns_400(self):
        res = self.client.post("/api/manual_spread/duplicate", json={})
        self.assertEqual(res.status_code, 400)
        self.assertIn("filename required", res.get_json().get("error", ""))

    def test_duplicate_nonexistent_trade_returns_404(self):
        with patch("blocks.stop.state.manual_spread_active_dir", return_value=self.active_dir):
            res = self.client.post(
                "/api/manual_spread/duplicate",
                json={"filename": "non_existent_trade.json"},
            )
            self.assertEqual(res.status_code, 404)

    def test_build_manual_trades_includes_expiry(self):
        """Verify build_manual_trades populates the expiry property from symbol."""
        with patch("manual_spread.entry.load_dashboard_manual_trades", return_value=[self.trade_data]):
            with patch("blocks.session.plan.load_manual_session_today", return_value=None):
                rows, day_pnl, open_count, day_slip = build_manual_trades(
                live_price_fn=lambda s: 1.0,
                phase_display_fn=lambda p, m: "Phase 1",
                trade_pnl_fn=lambda *a, **kw: (0.0, None, None, False),
                stop_label_fn=lambda a: "Stop @ 7.1",
                slot_state_fn=lambda *a, **kw: "open",
            )
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["expiry"], "260930")
            self.assertEqual(row["short_strike"], 7535)
            self.assertEqual(row["long_strike"], 7510)


if __name__ == "__main__":
    unittest.main()
