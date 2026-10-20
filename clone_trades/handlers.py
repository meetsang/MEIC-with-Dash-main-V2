"""Flask API route handlers for Discretionary Clone Trades."""
from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Dict

from flask import Blueprint, Flask, jsonify, request

from clone_trades.engine import CloneTradeEngine, get_engine
from clone_trades.monitor import get_monitor

log = logging.getLogger("clone_trades.handlers")


def _is_authorized(payload: Dict[str, Any], expected_secret: str) -> bool:
    if not expected_secret:
        return True

    # 1. Header X-Webhook-Secret
    hdr = request.headers.get("X-Webhook-Secret") or request.headers.get("X-Auth-Token") or ""
    if hdr and hmac.compare_digest(hdr.strip(), expected_secret):
        return True

    # 2. Query param ?secret=...
    qry = request.args.get("secret", "").strip()
    if qry and hmac.compare_digest(qry, expected_secret):
        return True

    # 3. Inside payload
    body_sec = str(payload.get("secret") or payload.get("token") or "").strip()
    if body_sec and hmac.compare_digest(body_sec, expected_secret):
        return True

    return False


def register_clone_trades_routes(app: Flask, engine: CloneTradeEngine = None, start_monitor: bool = True) -> None:
    eng = engine or get_engine()
    if start_monitor:
        monitor = get_monitor(eng)
        monitor.start()

    @app.route("/api/clone_trades/webhook", methods=["POST"])
    @app.route("/clone/webhook", methods=["POST"])
    def clone_webhook():
        raw = request.get_json(silent=True)
        if raw is None:
            raw = request.form.to_dict() or {}
            if request.data and not raw:
                try:
                    raw = json.loads(request.data.decode("utf-8", errors="replace"))
                except Exception:
                    raw = {}

        if not isinstance(raw, dict):
            return jsonify({"ok": False, "error": "Invalid payload format (expected JSON object)"}), 400

        # Check IP whitelist if configured
        if eng.config_mgr.allowed_ips:
            forwarded = request.headers.get("X-Forwarded-For", "")
            client_ip = forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")
            if client_ip not in eng.config_mgr.allowed_ips and client_ip not in ("127.0.0.1", "localhost", "::1"):
                log.warning("Blocked clone webhook request from unauthorized IP: %s", client_ip)
                return jsonify({"ok": False, "error": f"Forbidden: IP {client_ip} is not authorized"}), 403

        # Check authorization
        expected = eng.config_mgr.webhook_secret
        if expected and not _is_authorized(raw, expected):
            log.warning("Unauthorized clone webhook request")
            return jsonify({"ok": False, "error": "Unauthorized (invalid or missing secret)"}), 401

        res = eng.process_signal(raw)
        status_code = 200 if res.get("ok") else 400
        return jsonify(res), status_code

    @app.route("/api/clone_trades/status", methods=["GET"])
    def clone_status():
        return jsonify(eng.get_status())

    @app.route("/api/clone_trades/config", methods=["POST"])
    def clone_config_post():
        data = request.get_json(force=True, silent=True) or {}
        updated = eng.config_mgr.update(data)
        if "paper_mode" in data and hasattr(eng, "_resolve_broker"):
            eng._resolve_broker()
        updated_trades = []
        if "ladder_tiers" in data or "initial_stop_loss_pct" in data:
            updated_trades = eng.update_open_trades_ladder(eng.config_mgr.ladder_tiers)
        return jsonify({"ok": True, "config": updated, "updated_active_trades_count": len(updated_trades)})

    @app.route("/api/clone_trades/trade/<trade_id>/ladder", methods=["POST"])
    def clone_trade_ladder_post(trade_id: str):
        data = request.get_json(force=True, silent=True) or {}
        tiers_data = data.get("ladder_tiers", [])
        if not isinstance(tiers_data, list) or not tiers_data:
            return jsonify({"ok": False, "error": "ladder_tiers must be a non-empty list"}), 400
        res = eng.update_single_trade_ladder(trade_id, tiers_data)
        code = 200 if res.get("ok") else 400
        return jsonify(res), code

    @app.route("/api/clone_trades/signal/<sig_id>/take_manual", methods=["POST"])
    def clone_signal_take_manual(sig_id: str):
        data = request.get_json(force=True, silent=True) or {}
        custom_px = data.get("price")
        custom_qty = data.get("qty") or data.get("quantity")
        if custom_px is not None:
            try:
                custom_px = float(custom_px)
            except Exception:
                custom_px = None
        if custom_qty is not None:
            try:
                custom_qty = int(custom_qty)
            except Exception:
                custom_qty = None

        res = eng.take_signal_manually(sig_id, custom_price=custom_px, custom_qty=custom_qty)
        code = 200 if res.get("ok") else 400
        return jsonify(res), code

    @app.route("/api/clone_trades/signal/<sig_id>/leave", methods=["POST"])
    def clone_signal_leave(sig_id: str):
        res = eng.leave_signal(sig_id)
        code = 200 if res.get("ok") else 400
        return jsonify(res), code

    @app.route("/api/clone_trades/trade/<trade_id>/close", methods=["POST"])
    def clone_trade_close(trade_id: str):
        res = eng.close_trade_manual(trade_id)
        code = 200 if res.get("ok") else 400
        return jsonify(res), code

    @app.route("/api/clone_trades/trade/<trade_id>", methods=["GET"])
    def clone_trade_get(trade_id: str):
        trade = eng.get_trade(trade_id)
        if not trade:
            return jsonify({"ok": False, "error": f"Trade {trade_id} not found"}), 404
        return jsonify({"ok": True, "trade": trade})

    @app.route("/api/clone_trades/test_signal", methods=["POST"])
    def clone_test_signal():
        """Simulate an inbound webhook signal from the dashboard tester."""
        data = request.get_json(force=True, silent=True) or {}
        res = eng.process_signal(data)
        return jsonify(res)

    @app.route("/api/clone_trades/history", methods=["DELETE"])
    def clone_history_delete():
        scope = request.args.get("scope", "all")
        eng.clear_history(scope=scope)
        return jsonify({"ok": True, "message": f"History cleared (scope: {scope})"})
