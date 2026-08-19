# MNQ paper signal bot

Paper-trades Micro Nasdaq (`/MNQU6`) from a friend’s buy/sell webhook.

TastyTrade is used **only for prices**:
- REST `/market-data/by-type?future=/MNQU6` for the market-entry fill
- DXLink streamer symbol `/MNQU26:XCME` to watch the paper stop / profit target

**No orders are sent to TastyTrade.** Wins and losses are written to `mnq_paper/data/trades.jsonl`.

This module is not started by `run.py`. Keep it that way.

## Run

```powershell
cd MEIC-with-Dash-main-V2
uv run python -m mnq_paper --no-ngrok
```

Dashboard: http://127.0.0.1:8788

Webhook secret is generated on first run at `mnq_paper/data/webhook_secret.txt`, or set `MNQ_WEBHOOK_SECRET` in `.env`.

## ngrok (so your friend can POST from outside)

ngrok is not installed on this machine yet.

1. Install: https://ngrok.com/download or `winget install ngrok.ngrok`
2. `ngrok config add-authtoken <token>` (from the ngrok dashboard)
3. Restart without `--no-ngrok`:

```powershell
uv run python -m mnq_paper
```

The console prints `Friend webhook: POST https://….ngrok-free.app/signal`.

Or run ngrok yourself in another terminal:

```powershell
ngrok http 8788
```

## Friend payload

```powershell
curl -X POST https://YOUR-NGROK/signal `
  -H "X-Webhook-Secret: YOUR_SECRET" `
  -H "Content-Type: application/json" `
  -d '{"side":"buy"}'
```

`side` / `action` can be `buy`/`long` or `sell`/`short`. Optional fields:

```json
{
  "side": "sell",
  "qty": 1,
  "stop_points": 20,
  "target_points": 40,
  "note": "evening setup"
}
```

TradingView-style `{"strategy":{"order":{"action":"buy"}}}` also works.

Flatten the open paper trade: `POST /flatten` with the same secret.

One paper position at a time. A second signal while open returns HTTP 409.

## Defaults

| Setting | Default | Env |
|---|---|---|
| Symbol | `/MNQU6` | `MNQ_SYMBOL` |
| Stop | 20 points | `MNQ_STOP_POINTS` |
| Target | 40 points | `MNQ_TARGET_POINTS` |
| Qty | 1 | `MNQ_QTY` |
| Port | 8788 | `MNQ_PORT` |
| Point value | $2 | `MNQ_POINT_VALUE` |

Paper fill: **buy the ask / sell the bid** from the REST snapshot. If REST is rate-limited, the latest streamer quote is used instead.

A gap that crosses both stop and target in one tick counts as a **stop** (conservative).
