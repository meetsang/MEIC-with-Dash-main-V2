# MEIC Autotrader — SPX 0DTE Options Bot + Dashboard

Automated **0DTE SPX credit spread** trader with a live web dashboard. Supports **Charles Schwab** (legacy full-process mode) and **TastyTrade** (recommended — thin tranches + centralized stop monitor).

---

## Setup (do this first)

From the project root (`MEIC-with-Dash-main`):

### Recommended: [uv](https://docs.astral.sh/uv/) (virtual env + dependencies)

```powershell
cd C:\Users\meets\Downloads\MEIC\SPX\MEIC-with-Dash-main

# 0. Install uv (once per machine) — https://docs.astral.sh/uv/getting-started/installation/
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
# Restart the terminal, then:
uv --version

# 1. Create .venv and install all packages (reads pyproject.toml)
uv sync

# 2. Create config from template
copy .env.example .env
notepad .env

# 3. Verify install + credentials (uv run uses .venv automatically)
uv run python tests/adhoc_integration.py check-env
uv run python tests/adhoc_integration.py check-auth
```

`uv sync` creates a `**.venv**` folder in the project root (gitignored). You do **not** need to activate the venv if you prefix commands with `uv run`:

```powershell
uv run python run.py
uv run python tests/run_tests.py
```

To activate manually (optional):

```powershell
.\.venv\Scripts\Activate.ps1
python tests/adhoc_integration.py check-auth
```

### Alternative: pip + venv

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```


| Step                           | If it fails                                                                                              |
| ------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `uv` not found                 | Install uv (step 0 above) and restart the terminal                                                       |
| `uv sync`                      | Ensure Python 3.11+ is installed; try `uv python install 3.12`                                           |
| `No module named 'tastytrade'` | Run `uv sync` from project root (or `pip install -r requirements.txt`)                                   |
| `check-auth` fails             | Fill `TT_CLIENT_SECRET`, `TT_REFRESH_TOKEN`, `TT_ACCOUNT_NUMBER` in `.env` (TastyTrade) or Schwab tokens |
| `check-mqtt` fails             | Install and start Mosquitto (see [Prerequisites](#prerequisites) below)                                  |


`**.env` location:** same folder as `run.py` → `MEIC-with-Dash-main\.env`

---

## What It Does

- Trades **0DTE SPX weeklies** (put + call credit spreads) across up to 6 daily tranches
- Streams real-time **SPX / option prices** via MQTT
- Skips **NYSE holidays** and **FOMC days** (and the day after)
- **TastyTrade mode**: opens spreads quickly, writes JSON state, a **stop_monitor** manages all stop logic in parallel threads
- **Schwab mode**: original architecture (each tranche subprocess handles open + close)
- Dashboard at **[http://localhost:5002](http://localhost:5002)** — 12-slot tranche grid, live P&L, kill/pause controls, history (see [Dashboard](#dashboard))

---

## Architecture

### TastyTrade (recommended) — `BROKER=tastytrade`

```
run.py
├── dashboard/server.py          Flask UI (port 5002)
├── streaming/publish_tastytrade.py   DXLinkStreamer → MQTT (TASTYTRADE/*)
├── stop_monitor/run.py          Watches trades/active/*.json, one thread per spread
└── meic0dte/app_main.py         Thin tranche: open → write JSON → exit

meic0dte/trades/active/*.json  →  stop_monitor places stops, runs Phase 1/2/3 logic
meic0dte/trades/history/*.json →  completed spreads (archived from active/)
```

Stop monitor uses **MQTT streamer prices** for fast breach checks (~3s loop); broker order status is polled on a slower cadence (~60s). See [Stop Monitor](#stop-monitor-tastytrade) below.

### Schwab (legacy) — `BROKER=schwab`

```
run.py
├── dashboard/server.py
├── streaming/publish.py         Schwab WebSocket → MQTT (SCHWAB/*)
└── meic0dte/app_main.py         Full tranche: open + close in same process
```

---

## Prerequisites


| Requirement                                                                       | Schwab                           | TastyTrade            |
| --------------------------------------------------------------------------------- | -------------------------------- | --------------------- |
| Python 3.11+                                                                      | Yes                              | Yes                   |
| [uv](https://docs.astral.sh/uv/) (`uv sync`) or `pip install -r requirements.txt` | Yes                              | Yes                   |
| Mosquitto MQTT (`localhost:1883`)                                                 | Yes                              | Yes                   |
| Broker account with options                                                       | Schwab + dev app                 | TastyTrade + Open API |
| OS                                                                                | Windows tested; Linux OK for GCP | Same                  |


Dependencies are listed in `requirements.txt`. Key packages: `tastytrade` (TastyTrade API), `schwab-py` (Schwab), `paho-mqtt`, `flask`, `python-dotenv`.

Install Mosquitto:

```powershell
# Windows
winget install EclipseFoundation.Mosquitto
Start-Service -Name mosquitto

# Ubuntu (e.g. GCP e2-micro)
sudo apt-get install -y mosquitto
sudo systemctl enable --now mosquitto
```

---

## Configuration (`.env`)

Copy the template and edit:

```powershell
copy .env.example .env
```

```ini
# Which broker to use: schwab | tastytrade
BROKER=tastytrade

# --- Schwab (only if BROKER=schwab) ---
SCHWAB_CLIENT_ID=
SCHWAB_CLIENT_SECRET=
SCHWAB_ACCT=

# --- TastyTrade OAuth2 (live trading) ---
TT_CLIENT_SECRET=
TT_REFRESH_TOKEN=
TT_ACCOUNT_NUMBER=
TT_IS_TEST=false

# --- Paper trading (tastyware.dev, ~$30/mo, first month free) ---
PAPER_MODE=false
TASTYWARE_API_KEY=

TRADES_ACTIVE_DIR=trades/active
TRADES_CLOSED_DIR=trades/closed
```

Never commit `.env` — it is gitignored.

---

## Broker Setup Guides

### Option A: TastyTrade (recommended)

#### Step 1 — Open API access

1. Log in at [my.tastytrade.com](https://my.tastytrade.com)
2. Go to **Manage → My Profile → API**
3. Read and accept the Open API terms
4. Click **Create OAuth Application**
  - Callback URL: `http://localhost:8000` (required by tastytrade; you will not use it for server bots)
  - Enable scopes you need (account read/write, trade)
5. Save the **Client Secret** (shown once)

#### Step 2 — Create a refresh token

1. Same API page: **OAuth Applications → Manage → Create Grant**
2. Select your application and account
3. Copy the **Refresh Token** — in SDK v12+ it does not expire
4. Put in `.env`:
  ```
   TT_CLIENT_SECRET=your_client_secret
   TT_REFRESH_TOKEN=your_refresh_token
   TT_ACCOUNT_NUMBER=your_account_number
  ```

Account number: **Manage → Accounts** (e.g. `5WT00000`).

#### Step 3 — Optional: certification sandbox

For TastyTrade's official sandbox (resets daily, delayed quotes):

```
TT_IS_TEST=true
```

Use sandbox OAuth credentials from [developer.tastytrade.com](https://developer.tastytrade.com/sandbox).

#### Step 4 — Paper trading (tastyware)

Best for testing stops without live capital:

1. Sign up at [tastyware.dev](https://tastyware.dev) (~$30/mo, first month free)
2. Copy your **Paper API key**
3. In `.env`:
  ```
   PAPER_MODE=true
   TASTYWARE_API_KEY=your_key
  ```
4. Run with `--paper`:
  ```powershell
   python run.py --paper
  ```

Paper uses `PaperSession` + `PaperAlertStreamer`; live market data still comes from DXLink when not in pure simulation.

#### Verify TastyTrade setup

```powershell
python tests/adhoc_integration.py check-all --paper
```

---

### Option B: Schwab (legacy)

#### Step 1 — Developer app

1. [developer.schwab.com](https://developer.schwab.com) → create account
2. Create app with callback: `https://127.0.0.1:8080/callback`
3. Wait for approval (24–48 hours)
4. Note **App Key** and **App Secret**

#### Step 2 — `.env`

```
BROKER=schwab
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_CLIENT_SECRET=your_app_secret
SCHWAB_ACCT=your_10_digit_account
```

#### Step 3 — Initial OAuth token

```powershell
cd common/auth
python generate_token.py
```

1. Open the printed URL in a browser
2. Log in, approve access
3. Copy the full redirect URL from the address bar (connection error page is OK)
4. Paste into the terminal → creates `common/auth/token.json`

#### Step 4 — Run

```powershell
python run.py
```

Schwab access tokens refresh every **25 minutes** automatically while `run.py` runs. Refresh tokens last ~7 days — run the bot at least weekly or re-run `generate_token.py`.

#### Verify Schwab setup

```powershell
# In .env set BROKER=schwab first
python tests/adhoc_integration.py check-env
python tests/adhoc_integration.py check-auth
```

---

## Daily Operation

### TastyTrade (live)

```powershell
# .env: BROKER=tastytrade, PAPER_MODE=false, OAuth credentials set
python run.py
```

### TastyTrade (paper)

```powershell
python run.py --paper
```

### Schwab

```powershell
# .env: BROKER=schwab
python run.py
```

### Schedule (Central Time)

All times below are **US Central (CT)**. Tranche windows are defined in `run.py` (`TRANCHES`) and mirrored in `meic0dte/app/utilities.py` (`get_lot_time()`).

#### Daily timeline

| Time (CT) | What happens |
| --------- | ------------ |
| **8:20 AM** | Launcher wakes on weekdays (sleeps over weekends until Monday 8:20 AM) |
| **8:30 AM** | Streamer + `stop_monitor` start (if before 8:30, launcher waits) |
| **10:59 AM – 2:05 PM** | Six tranche windows — each opens **1 PCS + 1 CCS** (see table below) |
| **2:51 PM** | Phase 3: SPX proximity stop logic begins (`STRK_CHK_MIN` in `meic0dte/app/config.py`) |
| **3:00 PM** | Streamer + stop_monitor shut down until next trading day |

Weekends, NYSE holidays, and FOMC days (plus the day after FOMC) are skipped automatically.

#### Tranche windows (hard-coded)

Each window fires **once per day** the first time the clock is inside the range. A restart mid-window will still catch that tranche. Windows start **one minute before** the nominal lot time (legacy Task Scheduler tolerance); with a 5s poll loop the tranche typically fires within the first minute of the window.

| Lot name | Window start | Window end | Sides opened |
| -------- | ------------ | ---------- | ------------ |
| `11-00` | 10:59 AM | 11:05 AM | Put spread + Call spread |
| `12-00` | 11:59 AM | 12:05 PM | Put spread + Call spread |
| `12-30` | 12:29 PM | 12:35 PM | Put spread + Call spread |
| `01-15` | 1:14 PM | 1:20 PM | Put spread + Call spread |
| `01-45` | 1:44 PM | 1:50 PM | Put spread + Call spread |
| `02-00` | 1:59 PM | 2:05 PM | Put spread + Call spread |

To change these times, edit **both**:
1. `TRANCHES` in `run.py`
2. `TRANCHE_LOTS` in `dashboard/server.py` (the dashboard hardcodes the same lot names to build the 12-slot grid)

Keep them in sync — the dashboard will show a blank row for any lot that exists in one list but not the other.

#### 0DTE expiry (Monday's contract)

On a normal `python run.py` day, the bot trades **only that calendar day's SPXW expiry**:

- `get_expiration_date()` in `meic0dte/app/utilities.py` uses **today's date in Central time** (`YYMMDD`).
- On **Monday Jun 22**, all new spreads target **`260622`** contracts only.
- `MEIC_EXPIRY` in the environment overrides this (used only for integration/off-hours tests — do **not** set it in `.env` for live Monday trading).

Your integration-test orders (`477437936` PCS, `477437937` CCS) are already **Jun 22** contracts. Cancel them in TastyTrade if you do not want them to fill at the open — Monday's scheduled tranches will place **new** orders separately.

#### Contract quantity

Default size is **1 contract per spread leg** per tranche side:

- Set in `meic0dte/app/config.py` → `QUANTITY = 1`
- Each tranche places up to **2 spreads** (1 put credit spread + 1 call credit spread), each at qty 1
- Maximum new risk per tranche window: **2 spreads × 1 contract** (not 6 tranches × 5 contracts unless you change `QUANTITY`)

To change size, edit `QUANTITY` in `meic0dte/app/config.py` (there is no `.env` override today).

### Dashboard

Open **[http://localhost:5002](http://localhost:5002)** on the same PC where `run.py` is running. The Flask server binds to `127.0.0.1` only. To access from another device, change `host='127.0.0.1'` to `host='0.0.0.0'` in `dashboard/server.py` and allow port 5002 through your firewall.

`run.py` launches the dashboard automatically. To run it standalone (e.g. for testing with fixture files):

```powershell
python dashboard/server.py
```

#### Dashboard overview

Real-time single-page app (Flask + Socket.IO). The server pushes an update every **2 seconds** — no browser polling needed. The dashboard never calls the broker API directly.

| Source | Feeds |
| ------ | ----- |
| `meic0dte/trades/active/*.json` | Live trade state — strikes, credits, stop phases, heartbeats |
| MQTT `localhost:1883` | Live option prices and SPX for PnL calculation |
| `meic0dte/trades/heartbeat.json` | Stop monitor loop count and liveness |
| `dashboard/bot_status.json` | Launcher run/stop/skip status |
| `meic0dte/trades/pause_tranches.json` | Which tranche slots are paused |
| `dashboard/meic_trades.db` | SQLite closed trade history (built into Python, no install needed) |

**Tabs:** **Today** (live grid, controls, logs) · **History** (stats, 30-day chart, calendar, trade table)

#### Tranche grid

All 12 trade slots (6 lots x 2 sides) are shown at all times, even before any trades are placed. Slots are grouped by lot (e.g. `11-00 CT`) with Call and Put rows paired together.

Each row shows: Lot · Side · State · Short/Long strikes · Entry credit · Qty · Live short/long/spread prices · Live PnL · Stop phase · Stop price · Heartbeat

**State color coding:**

| State | Color | Meaning |
| ----- | ----- | ------- |
| `Pending` | Gray | Scheduled but not yet entered today |
| `Open` | Green | Active spread, stop_monitor running |
| `Closing` | Blue | Short leg filled; chasing long leg close |
| `Closed` | Gray | Fully closed, PnL final |
| `Killed` | Red | Manually closed via dashboard |
| `Paused` | Yellow | Skipped — will not enter this session |
| `Breached` | Orange | Market breach triggered close sequence |

**PnL formula (credit spreads):**

```
Live PnL = (entry credit - current spread) × 100 × qty
```

- Entered at 1.50, spread now 1.00 = +$50 profit (green)
- Entered at 1.50, spread now 2.00 = -$50 loss (red)
- Falls back to fill prices if MQTT is not streaming (shows ~$0 until streamer connects)

**Heartbeat dots** per row: green = last seen < 15s · yellow = 15-60s · red = > 60s or missing.

**Where the 12 slots come from:** hardcoded in `dashboard/server.py`:

```python
TRANCHE_LOTS  = ['11-00', '12-00', '12-30', '01-15', '01-45', '02-00']
TRANCHE_SIDES = ['C', 'P']
```

For each slot the server scans `meic0dte/trades/active/` for a matching JSON (matched by `state.lot` and `state.entry.side`). If found → live data. If not → `Pending` or `Paused`.

**If you add or remove a tranche, update this list to match `TRANCHES` in `run.py`.**

#### System health panel

| Indicator | Green when |
| --------- | ---------- |
| Launcher | `bot_status.json` state = `running` |
| Streamer | `stream_pub.log` written within last 30s |
| Stop Monitor | `heartbeat.json` timestamp within last 15s |
| SPX (MQTT) | Any live SPX price received |

#### Controls

| Button | Action |
| ------ | ------ |
| **Kill All Positions** | Writes `killswitch.json` — stop_monitor force-closes all active trades via limit chase (logged as `admin_killswitch`) |
| **Stop Bot** | Terminates the launcher; positions stay open with exchange stops |
| **Kill Selected** | Force-closes checked active trades; pauses checked pending slots |
| **Pause Selected** | Adds slots to `pause_tranches.json` — launcher skips those tranche windows |
| **Unpause Selected** | Removes slots from the pause file |
| **Close** (per row) | Writes a per-trade command file — stop_monitor force-closes that single trade (logged as `manual_close`) |

Kill All and per-trade Close reuse the existing breach pipeline — sentinel file written, stop_monitor picks it up within ~3 seconds, runs the limit-chase close. The `close_mechanism` field in each trade JSON distinguishes manual closes (`manual_close`, `admin_killswitch`) from market-triggered ones (`exchange_stop`, `software_breach`) for analytics.

**Pause** only skips future tranche entries — it has no effect on already-open trades.

---

## Stop Monitor (TastyTrade)

Each spread side gets a JSON file in `trades/active/`. The stop_monitor:

1. **Phase 1** — Places 2× short-leg stop; monitors fill/breach
2. **Phase 2** — When long leg ≤ $0.05, switches stop to 2× net credit
3. **Phase 3** — At 2:51 PM CT, market-closes short if SPX within $3 of strike

Run standalone (e.g. after manual seeding):

```powershell
python stop_monitor/run.py --paper
```

---

## Testing

**Full integration test guide:** [TESTING.md](TESTING.md) — off-hours tranche session, stop session on existing positions, breach/cr-db verification, command reference, and troubleshooting.

Quick start:

```powershell
uv run python tests/run_tests.py
uv run python tests/adhoc_integration.py check-all
```

See [TESTING.md](TESTING.md) for Scenarios 1–3 and all adhoc / `run.py` integration commands.

## Trade Parameters

Edit `meic0dte/app/config.py`:


| Parameter              | Default     | Description                 |
| ---------------------- | ----------- | --------------------------- |
| `QUANTITY`             | `1`         | Contracts per spread        |
| `SPREAD_WIDTH_MIN/MAX` | `25` / `35` | Spread width (points)       |
| `CREDIT_MIN`           | `0.90`      | Minimum credit ($)          |
| `CREDIT_MAX_P/C`       | `1.85`      | Maximum credit              |
| `STOP_PRCNT_P/C`       | `2.0`       | Stop multiplier (200%)      |
| `STRK_CHK_MIN`         | `51`        | Phase 3 starts at 2:51 PM   |
| `STRK_IDX_DIFF`        | `3`         | SPX proximity threshold ($) |


---

## Multi-Strategy (future)

`config/strategies.yaml` registers strategies. Iron Fly scaffold lives in `strategies/iron_fly/`. Enable when ready:

```yaml
  - name: Iron_Fly_SPX
    enabled: true
```

---

## Project Structure

```
.env / .env.example
run.py                         Main launcher
requirements.txt
secrets_template.json

brokers/
  base.py                      BrokerBase ABC
  tastytrade_broker.py         TastyTrade implementation
  paper_broker.py                Paper session wrapper

common/
  tt_config.py                 BROKER, OAuth, paper flags
  tt_auth.py                   Session factory
  broker_factory.py            get_broker(), streamer selection
  symbols.py                   Schwab ↔ TastyTrade symbology

streaming/
  publish.py                   Schwab streamer
  publish_tastytrade.py        TastyTrade streamer

stop_monitor/
  state.py                     JSON schema + atomic I/O
  phases.py                    Phase 1/2/3 plugins
  monitor.py                   Per-spread thread
  runner.py                    Parallel supervisor
  alerts.py                    AlertStreamer fills
  run.py                       CLI

meic0dte/
  app_main.py                  Tranche entry (thin or legacy)
  app/vertical.py              Schwab full tranche
  app/vertical_thin.py         TastyTrade thin tranche
  open/open_spread_tt.py       TastyTrade open + JSON write

trades/active/                 Open spread state (JSON)
trades/closed/                 Completed spreads

strategies/                    BaseStrategy + MEIC + Iron Fly
config/strategies.yaml

tests/
  run_tests.py                 Offline unit tests
  adhoc_integration.py         Broker smoke + trade + stop tests

TESTING.md                     Integration test scenarios & command reference

dashboard/server.py            Flask UI (port 5002)
```

---

## Deployment (GCP e2-micro)

1. Ubuntu minimal, 1 GB RAM — fits streamer + stop_monitor + dashboard (~495 MB peak)
2. Install Python 3.11+, Mosquitto, clone repo, `uv sync` or `pip install -r requirements.txt`
3. Copy `.env` with TastyTrade credentials
4. Use `systemd` units for `run.py` (or split streamer / stop_monitor / dashboard)
5. Open firewall only if exposing dashboard (port 5002)

---

## Troubleshooting


| Issue                      | Fix                                                               |
| -------------------------- | ----------------------------------------------------------------- |
| `TT_REFRESH_TOKEN` invalid | Re-create grant at my.tastytrade.com → API → Create Grant         |
| MQTT connection refused    | Start Mosquitto: `Get-Service mosquitto` (Windows)                |
| Schwab 401 errors          | Re-run `common/auth/generate_token.py`                            |
| No tranche fires           | Clock must be inside tranche window (CT); check `run.py` TRANCHES |
| stop_monitor idle          | Ensure JSON exists in `trades/active/` after thin tranche         |
| Paper fills instant        | Expected with tastyware PaperSession                              |


---

## Security

- Never commit `.env`, `token.json`, or `trades/active/*.json`
- OAuth refresh tokens are equivalent to passwords — restrict file permissions
- Use `--paper` / `PAPER_MODE=true` until you trust the full pipeline

