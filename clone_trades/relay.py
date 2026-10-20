"""
Dedicated Secure Micro-Relay for Discretionary Clone Trades.

Architecture & Security:
  1. Runs on an isolated port (default: 5005), physically separate from the main MEIC Dashboard (port 5002).
  2. ngrok or external tunnels should ONLY expose port 5005.
  3. Port 5002 (the dashboard UI, broker connections, order books, live positions, kill switches)
     is NEVER exposed to the public internet and remains strictly private on 127.0.0.1.
  4. Only accepts POST requests to /webhook (or /signal).
  5. Enforces two-tier authorization:
     - Secret token matching trades/clone_trades/webhook_secret.txt
     - (Optional) IP Whitelist matching allowed_ips in trades/clone_trades/config.json
  6. Verified signals are forwarded internally over loopback to http://127.0.0.1:5002.

Run:
  uv run python clone_trades/relay.py --ngrok
"""
from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Ensure project root is in sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from flask import Flask, jsonify, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [CLONE-RELAY] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("clone_trades.relay")


def load_secret(root_dir: str) -> str:
    """Load the shared secret token."""
    env_sec = os.environ.get("CLONE_WEBHOOK_SECRET", "").strip()
    if env_sec:
        return env_sec
    sec_path = os.path.join(root_dir, "trades", "clone_trades", "webhook_secret.txt")
    if os.path.exists(sec_path):
        try:
            with open(sec_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            pass
    return ""


def load_allowed_ips(root_dir: str) -> List[str]:
    """Load allowed IP whitelist from config.json if configured."""
    cfg_path = os.path.join(root_dir, "trades", "clone_trades", "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = data.get("allowed_ips", [])
            if isinstance(raw, list):
                return [str(x).strip() for x in raw if str(x).strip()]
        except Exception:
            pass
    return []


def create_relay_app(target_url: str, secret: str, allowed_ips: List[str]) -> Flask:
    app = Flask("clone_trades_relay")

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({
            "status": "ok",
            "service": "clone_trades_secure_relay",
            "ip_whitelist_active": len(allowed_ips) > 0,
            "secret_configured": bool(secret),
        }), 200

    @app.route("/webhook", methods=["POST"])
    @app.route("/signal", methods=["POST"])
    def relay_webhook():
        # 1. IP Whitelist check
        forwarded = request.headers.get("X-Forwarded-For", "")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (request.remote_addr or "")

        if allowed_ips and client_ip not in allowed_ips and client_ip not in ("127.0.0.1", "localhost", "::1"):
            log.warning("REJECTED: Request from unauthorized IP: %s", client_ip)
            return jsonify({
                "ok": False,
                "error": f"Forbidden: IP {client_ip} is not in the authorized sender whitelist",
            }), 403

        # 2. Secret authentication check
        req_token = (
            request.headers.get("X-Webhook-Secret")
            or request.headers.get("X-Auth-Token")
            or request.args.get("secret")
            or ""
        ).strip()

        raw_json = request.get_json(silent=True) or {}
        if not req_token and isinstance(raw_json, dict):
            req_token = str(raw_json.get("secret") or raw_json.get("token") or "").strip()

        if secret and (not req_token or not hmac.compare_digest(req_token, secret)):
            log.warning("REJECTED: Unauthorized webhook attempt from IP %s (invalid or missing secret)", client_ip)
            return jsonify({
                "ok": False,
                "error": "Unauthorized: Invalid or missing secret token",
            }), 401

        # 3. Forward verified payload internally to MEIC dashboard
        endpoint = f"{target_url.rstrip('/')}/api/clone_trades/webhook"
        data_bytes = json.dumps(raw_json).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Webhook-Secret": secret,
            "X-Forwarded-For": client_ip,
        }

        try:
            req = Request(endpoint, data=data_bytes, headers=headers, method="POST")
            with urlopen(req, timeout=10) as resp:
                resp_code = resp.status
                resp_body = resp.read().decode("utf-8")
                try:
                    resp_json = json.loads(resp_body)
                    return jsonify(resp_json), resp_code
                except Exception:
                    return resp_body, resp_code, {"Content-Type": "application/json"}
        except HTTPError as http_err:
            body = http_err.read().decode("utf-8", errors="replace")
            try:
                return jsonify(json.loads(body)), http_err.code
            except Exception:
                return jsonify({"ok": False, "error": f"Internal error from dashboard: {body}"}), http_err.code
        except URLError as url_err:
            log.error("Failed to reach internal MEIC dashboard at %s: %s", endpoint, url_err.reason)
            return jsonify({
                "ok": False,
                "error": f"MEIC Dashboard (port 5002) is not responding: {url_err.reason}. Ensure run.py or dashboard/server.py is running.",
            }), 502
        except Exception as exc:
            log.error("Relay forwarding error: %s", exc)
            return jsonify({"ok": False, "error": f"Relay error: {exc}"}), 500

    # Block all other paths with 404
    @app.errorhandler(404)
    def handle_404(e):
        return jsonify({"ok": False, "error": "Endpoint not found"}), 404

    return app


def main():
    parser = argparse.ArgumentParser(description="Secure Micro-Relay for Discretionary Clone Trades")
    parser.add_argument("--port", type=int, default=5005, help="Port to listen on (default: 5005)")
    parser.add_argument("--target", type=str, default="http://127.0.0.1:5002", help="Target MEIC dashboard URL")
    parser.add_argument("--ngrok", action="store_true", help="Automatically launch an ngrok tunnel to this relay")
    parser.add_argument("--authtoken", type=str, default="", help="Optional ngrok authtoken")
    parser.add_argument("--domain", type=str, default="", help="Optional static ngrok domain (e.g. my-relay.ngrok-free.app)")
    args = parser.parse_args()

    secret = load_secret(ROOT)
    allowed_ips = load_allowed_ips(ROOT)

    print("\n" + "=" * 76)
    print("  MEIC DISCRETIONARY CLONE TRADES - SECURE RELAY")
    print("=" * 76)
    print(f"  * Local Relay Port      : {args.port}")
    print(f"  * Internal Target       : {args.target}")
    print(f"  * Secret Token Active   : {'YES (' + secret[:6] + '...' + secret[-4:] + ')' if secret else 'NO (WARNING: Unprotected!)'}")
    print(f"  * Allowed Sender IPs    : {allowed_ips if allowed_ips else 'ALL (open to any IP with valid secret)'}")
    print(f"  * Port 5002 Exposure    : ZERO (port 5002 remains strictly private on localhost)")
    print("=" * 76)

    public_url = None
    if args.ngrok:
        try:
            from mnq_paper.ngrok_tunnel import start_ngrok
            public_url = start_ngrok(args.port, authtoken=args.authtoken, domain=args.domain)
            if public_url:
                print("\n  [+] SECURE TUNNEL ONLINE!")
                print(f"  [+] Public Webhook URL : {public_url}/webhook")
                print("\n  Share this with your friend:")
                print(f"  1. Webhook URL : {public_url}/webhook")
                print(f"  2. Header      : X-Webhook-Secret: {secret}")
                print("\n  Sample Curl command for your friend to test:")
                print(f'  curl -X POST "{public_url}/webhook" \\')
                print(f'    -H "Content-Type: application/json" \\')
                print(f'    -H "X-Webhook-Secret: {secret}" \\')
                print('    -d \'{"ticker":"SPY","expiration":"2026-09-25","strike":771,"call_put":"call","price":1.00}\'')
                print("\n" + "=" * 76 + "\n")
        except Exception as exc:
            log.warning("Could not start ngrok automatically: %s", exc)

    if not public_url:
        print(f"\n  To expose this relay to your friend without exposing port 5002:")
        print(f"  Run in another terminal: ngrok http {args.port}")
        print("\n" + "=" * 76 + "\n")

    app = create_relay_app(args.target, secret, allowed_ips)
    # Run relay server (quiet werkzeug logging)
    import werkzeug.serving
    werkzeug.serving.WSGIRequestHandler.log_request = lambda self, *a, **kw: None
    app.run(host="0.0.0.0", port=args.port, debug=False)


if __name__ == "__main__":
    main()
