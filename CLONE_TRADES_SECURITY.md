# Discretionary Clone Trades — Security & Remote Webhook Guide

This document outlines the security architecture, setup instructions, and friend integration specs for the **MEIC Discretionary Clone Trades** system.

---

## 1. Security Overview & Architecture

### The Risk We Prevented
Port `5002` hosts your MEIC Dashboard, broker session credentials, order books, positions, and live strategy kill switches. **Port 5002 is never exposed to the public internet.**

### The 3-Tier Security Model
```
┌──────────────────────────────┐
│   Friend in Another City     │
│ (TradingView / Python Script) │
└──────────────┬───────────────┘
               │ HTTPS POST /webhook
               │ + X-Webhook-Secret: <token>
               ▼
┌──────────────────────────────┐
│     ngrok Public Tunnel      │
└──────────────┬───────────────┘
               │ Forwards ONLY to Port 5005
               ▼
┌─────────────────────────────────────────────────────────────┐
│              Dedicated Secure Relay (Port 5005)              │
│  - No web dashboard, no admin tools, no broker connections   │
│  - Validates Secret Token (Constant-time HMAC)              │
│  - (Optional) Validates Friend's Public IP Whitelist        │
│  - Rejects any other path with 404/403                      │
└──────────────┬──────────────────────────────────────────────┘
               │ Loopback 127.0.0.1 ONLY
               ▼
┌─────────────────────────────────────────────────────────────┐
│              Internal MEIC Dashboard (Port 5002)             │
│  - 100% PRIVATE on your PC (unreachable from the internet)   │
│  - Chase Guard (25% upper limit check)                      │
│  - Floor Guard (75% lower crash protection check)           │
│  - Capital Allocation ($500 limit sizing)                   │
│  - Market Hours Gate (Off-hours protection)                 │
│  - Straight Away Trades switch (Manual review vs Auto)      │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Do You Have to Always Run This?

### When to Run
- **Only when you want to receive trades**: You only need to run the relay during market trading sessions (e.g. 9:30 AM – 4:00 PM ET) when your friend is actively trading.
- **You do NOT need to leave it running 24/7**: When the market closes or you are done trading for the day, you can close the relay window anytime. If your friend sends a signal while the relay is closed, it will simply fail gracefully on his end.

### How to Start It
You have three easy ways to run the relay:
1. **Automatic with Windows Task Scheduler (Default)**:
   Your scheduled job:
   ```cmd
   cmd /c cd /d C:\Users\User\OneDrive\Desktop\Projects\2026\MEIC-with-Dash-main-V2 && .venv\Scripts\python.exe run.py --one-day
   ```
   Now **automatically launches the clone relay and ngrok tunnel** in the morning alongside the dashboard, and automatically terminates them at end of day! No manual intervention needed.
2. **1-Click**: Double-click `run_clone_relay.bat` in the project root folder.
3. **Manual Terminal**:
   ```powershell
   uv run python clone_trades/relay.py --ngrok
   ```

### Pro-Tip: Permanent Free ngrok Domain (Never changes)
By default, free ngrok generates a random URL each time it starts (`abc-123.ngrok-free.app`), meaning your friend would have to update his webhook URL every time you restart.

To give your friend a permanent URL that **never changes**:
1. Log into your free account at [dashboard.ngrok.com](https://dashboard.ngrok.com).
2. Go to **Cloud Edge -> Domains** and click **Create Domain** (every free account gets 1 free static domain, e.g. `fast-hawk-ready.ngrok-free.app`).
3. Start the relay with your permanent domain:
   ```powershell
   uv run python clone_trades/relay.py --ngrok --domain fast-hawk-ready.ngrok-free.app
   ```
4. Now your friend keeps that same URL forever, regardless of how many times you stop or start the relay.

---

## 3. Two-Tier Authorization ("Only Him Can Raise")

### Gate 1: 32-Character Webhook Secret
- Located on your machine at:
  `trades/clone_trades/webhook_secret.txt`
- The relay and dashboard require your friend to pass this exact secret in the `X-Webhook-Secret` header.
- Any request without this exact secret is rejected immediately with `401 Unauthorized`.
- It uses timing-safe HMAC (`hmac.compare_digest`) to prevent brute-force or timing attacks.

### Gate 2: Friend IP Whitelist (Recommended)
You can lock the system so it rejects requests from all IP addresses except your friend's:
1. Ask your friend for his public IP address (he can check [whatismyip.com](https://www.whatismyip.com)).
2. Open `trades/clone_trades/config.json` and add his IP:
   ```json
   {
     "allowed_ips": ["73.14.55.120"]
   }
   ```
3. Even if your webhook secret were somehow intercepted, anyone else trying to send a trade will receive `403 Forbidden`.

---

## 4. Financial & Trading Guardrails (Built-in Safety)

Even if a valid webhook comes in from your friend, the following guardrails protect your broker account:

1. **Straight Away Trades Disarmed (`auto_take_trades: false`)**:
   - By default, incoming webhooks are placed into your **Pending Decisions** list on the "Discretionary Clone Trades" tab.
   - You see the ticker, strike, alert price, and live market price. You decide whether to click **Take Trade** or leave it.
   - Only when you turn ON "Straight Away Trades" in the UI will it automatically place orders with your broker.
2. **25% Chase Guard**:
   - If your friend alerted at `$1.00`, but by the time the order is processed the market ask is `$1.30` (+30%), the system automatically flags the trade as `CHASE_EXCEEDED` and halts execution.
3. **75% Floor Guard (Late Alert / Collapsed Option Protection)**:
   - If your friend alerted at `$0.98`, but by the time the alert is processed the market has collapsed to `$0.05` (-94.9%), the system automatically flags the trade as `FLOOR_EXCEEDED` and halts auto-execution to prevent taking dead or stale positions on your brokerage. Configurable on screen (default 75%).
4. **Fixed Allocation Sizing ($500)**:
   - Sizing is automatically capped to your configured budget (e.g. $500 max per trade).
4. **Market Hours Gate**:
   - Prevents orders from being sent when the market is closed (9:30 AM – 4:00 PM ET) unless you explicitly turn on "Allow Off-Hours Orders".
5. **Laddered OCO Stops & Targets (Percentage Scaling Out)**:
   - **Profit Targets (PT)**: Scales out a configurable **% of trades** at each stage (e.g., 33.3% at Tier 1, 33.3% at Tier 2, 33.4% at Tier 3).
   - **Stop Loss (SL)**: Always protects **100%** of all remaining contracts and automatically ratchets tighter (e.g. to breakeven or profit lock) as targets are reached.

---

## 5. Friend's Integration Specification

Share the following specs with your friend so he can configure his TradingView alert or Python script:

### Request Format
- **HTTP Method**: `POST`
- **URL**: `https://<your-ngrok-domain>.ngrok-free.app/webhook`
- **Headers**:
  ```http
  Content-Type: application/json
  X-Webhook-Secret: <your_secret_from_webhook_secret.txt>
  ```

### JSON Body Fields
| Field | Type | Description | Example |
|---|---|---|---|
| `ticker` | string | Underlying symbol | `"SPY"`, `"QQQ"`, `"IWM"` |
| `expiration` | string | Expiration date (`YYYY-MM-DD` or `MM/DD/YYYY`) | `"2026-09-25"` |
| `strike` | number | Option strike price | `771` or `771.5` |
| `call_put` | string | Option type (`call` or `put`) | `"call"` |
| `price` | number | Alert / entry price | `1.00` |
| `action` | string | *(Optional)* Action (`buy` or `sell`) | `"buy"` (default) |

### Sample Payload
```json
{
  "ticker": "SPY",
  "expiration": "2026-09-25",
  "strike": 771,
  "call_put": "call",
  "price": 1.00
}
```

### Sample cURL Command (for testing)
```bash
curl -X POST "https://<your-ngrok-domain>.ngrok-free.app/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: 80f581ca02a6d956a7e67f84f28da529" \
  -d '{"ticker":"SPY","expiration":"2026-09-25","strike":771,"call_put":"call","price":1.00}'
```

### Possible Response Codes
- `200 OK`: Webhook received, authorized, and processed successfully.
- `401 Unauthorized`: Missing or incorrect `X-Webhook-Secret`.
- `403 Forbidden`: Sender IP address is not in your `allowed_ips` whitelist.
- `400 Bad Request`: Payload missing required fields or market conditions violated.
- `502 Bad Gateway`: The internal MEIC dashboard on port 5002 is not running.
