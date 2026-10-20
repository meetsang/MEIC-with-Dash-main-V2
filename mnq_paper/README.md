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

## ngrok (External Webhook Tunnel)

ngrok is installed on this machine. To allow your friend or TradingView to POST signals from outside your local network:

### 1. Get your free authtoken
- Sign up or log in at [dashboard.ngrok.com](https://dashboard.ngrok.com/signup)
- Copy your token from [Your Authtoken](https://dashboard.ngrok.com/get-started/your-authtoken)

### 2. Configure your authtoken (Choose one)

**Option A: In `.env` (Recommended)**
Add to your `.env` file:
```env
NGROK_AUTHTOKEN=your_token_here
```

**Option B: Via CLI**
In PowerShell / terminal:
```powershell
ngrok config add-authtoken your_token_here
```

### 3. Start the Bot with ngrok
Run without `--no-ngrok`:
```powershell
uv run python -m mnq_paper
```
The console will automatically print the public URL:
```text
[MNQ-PAPER] INFO Starting ngrok: ngrok http 8788
[MNQ-PAPER] INFO ngrok public URL: https://xxxx-xx-xx.ngrok-free.app
[MNQ-PAPER] INFO Friend webhook: POST https://xxxx-xx-xx.ngrok-free.app/signal
```

To run locally without an ngrok tunnel (local LAN only):
```powershell
uv run python -m mnq_paper --no-ngrok
```

## Friend payloads

### Bullish (Buy)
```json
{
  "aname": "pavi",
  "account": "Sim101",
  "ticker": "NQ1!",
  "action": "buy",
  "quantity": "1",
  "price": "{{close}}",
  "strategy": "apexnq"
}
```

### Bearish (Sell)
```json
{
  "aname": "pavi",
  "account": "Sim101",
  "ticker": "NQ1!",
  "action": "sell",
  "quantity": "1",
  "price": "{{close}}",
  "strategy": "apexnq"
}
```

### Supported Variations:
- `action` / `side`: `buy`, `sell`, `long`, `short`, `bullish`, `bearish`, `bull`, `bear`
- `quantity` / `qty`: number or string (e.g. `"1"`, `1`)
- `price`: `{{close}}` uses live market fill; a numeric price is logged as the alert trigger price
- `strategy`: defaults to `apexnq` (or configured via `MNQ_STRATEGY`)

Webhook cURL test:
```powershell
curl -X POST http://127.0.0.1:8788/signal `
  -H "X-Webhook-Secret: YOUR_SECRET" `
  -H "Content-Type: application/json" `
  -d '{"aname":"pavi","account":"Sim101","ticker":"NQ1!","action":"buy","quantity":"1","price":"{{close}}","strategy":"apexnq"}'
```

Flatten the open paper trade: `POST /flatten` with the same secret, or click the **Flatten** button on the dashboard.

One paper position at a time. A second signal while a trade is open returns HTTP 409.

## Defaults

| Setting | Default | Env |
|---|---|---|
| Symbol | `/MNQU6` | `MNQ_SYMBOL` |
| Stop Loss | 40 points | `MNQ_STOP_POINTS` |
| Profit Target | 25 points | `MNQ_TARGET_POINTS` |
| Strategy | `apexnq` | `MNQ_STRATEGY` |
| Qty | 1 | `MNQ_QTY` |
| Port | 8788 | `MNQ_PORT` |
| Point value | $2 | `MNQ_POINT_VALUE` |

Paper fill: **buy the ask / sell the bid** from the REST snapshot. If REST is rate-limited, the latest streamer quote is used instead.

A gap that crosses both stop and target in one tick counts as a **stop** (conservative).

All entries and exits are journaled in `mnq_paper/data/trades.jsonl` with timestamps for:
- `signal_received_at`: Exact timestamp when the webhook hit
- `opened_at`: Time trade was filled
- `closed_at`: Time profit target or stop loss was triggered by streamer

