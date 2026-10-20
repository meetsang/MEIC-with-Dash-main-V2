"""API endpoint tests for Discretionary Clone Trades."""
from __future__ import annotations

import json
import tempfile
import unittest

from flask import Flask

from clone_trades.config import CloneTradeConfigManager
from clone_trades.engine import CloneTradeEngine
from clone_trades.handlers import register_clone_trades_routes
from clone_trades.quotes import OptionQuoteService


class TestCloneTradesAPI(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.config_mgr = CloneTradeConfigManager(root_dir=self.tmp_dir.name)
        self.config_mgr.paper_mode = True
        self.config_mgr.webhook_secret = "test_secret_123"
        self.quote_service = OptionQuoteService()
        self.engine = CloneTradeEngine(
            config_mgr=self.config_mgr,
            quote_service=self.quote_service,
        )

        self.app = Flask(__name__)
        register_clone_trades_routes(self.app, engine=self.engine, start_monitor=False)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_status_endpoint(self):
        res = self.client.get("/api/clone_trades/status")
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("config", data)
        self.assertIn("active_trades", data)
        self.assertIn("signals", data)
        self.assertIn("stats", data)
        self.assertIn("today_date_cst", data)
        self.assertIn("today_signals", data)
        self.assertIn("historical_signals", data)
        self.assertIn("broker_info", data)

    def test_config_update_endpoint(self):
        res = self.client.post(
            "/api/clone_trades/config",
            json={"auto_take_trades": True, "allocation_amount": 750.0, "max_premium_pct": 30.0, "floor_guard_pct": 60.0},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["config"]["auto_take_trades"])
        self.assertEqual(data["config"]["allocation_amount"], 750.0)
        self.assertEqual(data["config"]["max_premium_pct"], 30.0)
        self.assertEqual(data["config"]["floor_guard_pct"], 60.0)

    def test_webhook_auth_rejected_without_secret(self):
        res = self.client.post(
            "/api/clone_trades/webhook",
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80},
        )
        self.assertEqual(res.status_code, 401)

    def test_webhook_auth_accepted_with_secret_header(self):
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)

        res = self.client.post(
            "/api/clone_trades/webhook",
            headers={"X-Webhook-Secret": "test_secret_123"},
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "TAKEN")

    def test_test_signal_endpoint_bypasses_secret(self):
        """Dashboard test simulator can trigger test signals directly."""
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 0.80)

        res = self.client.post(
            "/api/clone_trades/test_signal",
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 0.80},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["status"], "TAKEN")

    def test_manual_close_trade(self):
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        res = self.client.post(
            "/api/clone_trades/test_signal",
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00},
        )
        trade_id = res.get_json()["trade"]["id"]

        close_res = self.client.post(f"/api/clone_trades/trade/{trade_id}/close")
        self.assertEqual(close_res.status_code, 200)
        self.assertTrue(close_res.get_json()["ok"])
        self.assertEqual(len(self.engine.active_trades), 0)

    def test_update_trade_ladder_endpoint(self):
        """Test POST /api/clone_trades/trade/<trade_id>/ladder modifies position ladder."""
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        res = self.client.post(
            "/api/clone_trades/test_signal",
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00},
        )
        trade_id = res.get_json()["trade"]["id"]

        new_ladder = [
            {"tier_id": 1, "target_profit_pct": 35.0, "exit_pct": 50.0, "stop_loss_pct": -45.0, "description": "T1"},
            {"tier_id": 2, "target_profit_pct": 75.0, "exit_pct": 50.0, "stop_loss_pct": 0.0, "description": "T2"},
        ]
        ladder_res = self.client.post(
            f"/api/clone_trades/trade/{trade_id}/ladder",
            json={"ladder_tiers": new_ladder},
        )
        self.assertEqual(ladder_res.status_code, 200)
        data = ladder_res.get_json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["active_pt_price"], 1.35)
        self.assertEqual(data["active_stop_price"], 0.55)
        self.assertEqual(data["active_stop_pct"], -45.0)

        # Check status endpoint reflects it
        status_res = self.client.get("/api/clone_trades/status")
        active = status_res.get_json()["active_trades"]
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["active_pt_price"], 1.35)
        self.assertEqual(active[0]["active_stop_price"], 0.55)

    def test_get_trade_endpoint(self):
        """Test GET /api/clone_trades/trade/<trade_id> retrieves full trade details."""
        self.config_mgr.auto_take_trades = True
        self.quote_service.set_mock_price(".SPY260925C500", 1.00)

        res = self.client.post(
            "/api/clone_trades/test_signal",
            json={"ticker": "SPY", "exp": "2026-09-25", "strike": 500, "type": "CALL", "price": 1.00},
        )
        trade_id = res.get_json()["trade"]["id"]

        get_res = self.client.get(f"/api/clone_trades/trade/{trade_id}")
        self.assertEqual(get_res.status_code, 200)
        t_data = get_res.get_json()
        self.assertTrue(t_data["ok"])
        self.assertEqual(t_data["trade"]["id"], trade_id)
        self.assertEqual(t_data["trade"]["spec"]["option_type"], "CALL")

        # Nonexistent trade returns 404
        not_found = self.client.get("/api/clone_trades/trade/nonexistent_id")
        self.assertEqual(not_found.status_code, 404)
        self.assertFalse(not_found.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
