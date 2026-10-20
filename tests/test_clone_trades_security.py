"""Unit tests for Clone Trades Security: IP whitelisting, secret authentication, and relay isolation."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from flask import Flask

from clone_trades.config import CloneTradeConfigManager
from clone_trades.engine import CloneTradeEngine
from clone_trades.handlers import register_clone_trades_routes
from clone_trades.relay import create_relay_app


@pytest.fixture
def mock_engine(tmp_path):
    root = str(tmp_path)
    cfg_mgr = CloneTradeConfigManager(root)
    cfg_mgr.webhook_secret = "super_secret_token_123"
    cfg_mgr.allowed_ips = ["198.51.100.42"]  # Friend's mock IP
    cfg_mgr.paper_mode = True

    engine = CloneTradeEngine(config_mgr=cfg_mgr)
    return engine


@pytest.fixture
def test_app(mock_engine):
    app = Flask("test_security_app")
    register_clone_trades_routes(app, engine=mock_engine, start_monitor=False)
    return app.test_client()


def test_secret_authentication_required(test_app):
    # Missing secret -> 401
    resp = test_app.post(
        "/api/clone_trades/webhook",
        json={"ticker": "SPY", "expiration": "2026-09-25", "strike": 500, "call_put": "call", "price": 1.0},
        headers={"X-Forwarded-For": "198.51.100.42"},
    )
    assert resp.status_code == 401
    assert "Unauthorized" in resp.get_json()["error"]


def test_ip_whitelist_blocks_stranger(test_app):
    # Correct secret, but stranger's IP -> 403
    resp = test_app.post(
        "/api/clone_trades/webhook",
        json={"ticker": "SPY", "expiration": "2026-09-25", "strike": 500, "call_put": "call", "price": 1.0},
        headers={
            "X-Webhook-Secret": "super_secret_token_123",
            "X-Forwarded-For": "203.0.113.99",  # Random internet IP
        },
    )
    assert resp.status_code == 403
    assert "Forbidden" in resp.get_json()["error"]


def test_ip_whitelist_allows_friend_ip(test_app):
    # Correct secret + friend's IP -> processed
    resp = test_app.post(
        "/api/clone_trades/webhook",
        json={"ticker": "SPY", "expiration": "2026-09-25", "strike": 500, "call_put": "call", "price": 1.0},
        headers={
            "X-Webhook-Secret": "super_secret_token_123",
            "X-Forwarded-For": "198.51.100.42",  # Friend's IP
        },
    )
    # Passed security check (either 200 or 400 depending on symbol/broker, but NOT 401 or 403)
    assert resp.status_code in (200, 400)
    assert resp.status_code != 401
    assert resp.status_code != 403


def test_relay_app_blocks_strangers_and_protects_dashboard():
    relay_app = create_relay_app(
        target_url="http://127.0.0.1:5002",
        secret="test_secret",
        allowed_ips=["198.51.100.42"],
    )
    client = relay_app.test_client()

    # 1. Non-existent path returns 404 (no dashboard, no admin, no file access)
    assert client.get("/").status_code == 404
    assert client.get("/api/strategy/MEIC_IC/close").status_code == 404
    assert client.get("/dashboard").status_code == 404

    # 2. Health check
    h = client.get("/health")
    assert h.status_code == 200
    assert h.get_json()["service"] == "clone_trades_secure_relay"
    assert h.get_json()["ip_whitelist_active"] is True

    # 3. Post to /webhook from unauthorized IP -> 403
    r1 = client.post(
        "/webhook",
        json={"ticker": "SPY"},
        headers={"X-Forwarded-For": "1.2.3.4", "X-Webhook-Secret": "test_secret"},
    )
    assert r1.status_code == 403
    assert "Forbidden" in r1.get_json()["error"]

    # 4. Post to /webhook with invalid secret -> 401
    r2 = client.post(
        "/webhook",
        json={"ticker": "SPY"},
        headers={"X-Forwarded-For": "198.51.100.42", "X-Webhook-Secret": "wrong_secret"},
    )
    assert r2.status_code == 401
    assert "Unauthorized" in r2.get_json()["error"]
