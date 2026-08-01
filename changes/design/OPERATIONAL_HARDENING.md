# Operational Hardening & Path to Perfection

**Date**: Jun 22, 2026  
**Status**: Living document — observations from first production launch  
**Audience**: Operators and future dev work  
**Related**: [GAP_ANALYSIS.md](GAP_ANALYSIS.md), [DASHBOARD_IMPLEMENTATION.md](DASHBOARD_IMPLEMENTATION.md), [README.md](../README.md)

---

## Purpose

This document captures findings from the Jun 22, 2026 production launch (`uv run python run.py`). It explains behaviors that look surprising in logs or on the dashboard, records what was fixed vs. what remains, and prioritizes improvements toward a production-grade operator experience — without changing runtime code until planned.

---

## Executive Summary

| Area | Status | Notes |
|------|--------|-------|
| Core trading path | **OK** | Tranches, streamer, stop_monitor, MQTT prices, broker auth verified |
| Dashboard health dots | **Fixed** (needs restart) | Was checking Schwab log path + `SCHWAB/SPX`; now `TASTYTRADE/` |
| Console noise | **Cosmetic** | TastyTrade SDK logs every quote at INFO to stdout |
| Stream log panel | **Suboptimal** | Reads entire log file every 5s; file grows fast |
| `optsymbols.json` at 8:54 | **Expected but confusing** | Stale symbols from prior runs; streamer starts at 8:30, not 11:00 |
| GAP-05 breach math | **Confirmed** | Documented in GAP_ANALYSIS with CCS 7525/7550 example |
| Navbar **Connecting…** | **Cosmetic / Socket.IO** | WebSocket client not connected; REST fallback may still show SPX |
| Bot Status **Stopped** | **Misleading label** | Card tracks dashboard-spawned bot only, not external `run.py` |

---

## 1. Dashboard — System Health Panel

### StopMon `#N` — what it means

**StopMon #8** (or any number) is **not** “8 trades” or “8 monitors.”

It is the **supervisor loop counter** from `stop_monitor/runner.py`:

- Every **3 seconds**, the supervisor:
  1. Scans `meic0dte/trades/active/*.json` for new spreads
  2. Starts a `StopMonitor` thread per active trade
  3. Writes `meic0dte/trades/heartbeat.json`

| Field in `heartbeat.json` | Meaning |
|---------------------------|---------|
| `loop_count` | Shown as **StopMon #N** on dashboard |
| `ts` | Last heartbeat time — drives green/yellow/red dot |
| `active_trades` | Count of JSON files currently monitored (not shown in label) |

**Dot colors** (from `heartbeat.json` `ts`):

| Color | Age of last heartbeat |
|-------|------------------------|
| Green | &lt; 15 seconds |
| Yellow | 15–60 seconds |
| Red | &gt; 60 seconds |

**Separate from grid row heartbeats**: Each open trade row can show its own small dot from `recovery.last_heartbeat` inside that trade’s JSON — that is the **per-spread** monitor thread, not the supervisor.

### Streamer dot

Green when `stream_pub_tt.log` (TastyTrade) was modified within **30 seconds**.  
Previously grey because dashboard checked legacy `streaming/stream_pub.log` (Schwab path, file absent).

### SPX price in health bar

Comes from MQTT topic `TASTYTRADE/SPX`.  
Previously showed `–` because dashboard looked up `SCHWAB/SPX`.

**Fix applied** in `dashboard/server.py` (Jun 22). Requires **dashboard process restart** to load.

### Navbar — **Connecting…** (top right)

This indicator is **only** the **Socket.IO WebSocket** connection between your browser and `dashboard/server.py`. It is **not** streamer health, MQTT, or `run.py` status.

| Label | Meaning |
|-------|---------|
| **Connecting…** | Default on page load; Socket.IO has not fired `connect` yet — or **never will** (stuck) |
| **Live** | Socket.IO connected; server pushes `update` events every **2 seconds** |
| **Disconnected** | Socket.IO was connected and dropped |

**Implementation** (`dashboard/templates/index.html`):

```javascript
socket = io();                          // same-origin WebSocket to :5002
socket.on('connect',    () => 'Live');
socket.on('disconnect', () => 'Disconnected');
// No connect_error handler → failed connects stay "Connecting…" forever
```

#### Why you can see SPX price but still **Connecting…**

The dashboard uses **two independent data paths**:

| Path | What it updates | When |
|------|-----------------|------|
| **REST** `GET /api/summary` | SPX, grid, system health, PnL | **Once** on page load |
| **Socket.IO** `update` event | Same fields | Every **2s** while **Live** |

So a stuck **Connecting…** means:

- The **first paint** can look healthy (SPX 7526.88, green System Health) from the one-time REST call.
- **Live refresh stops** — grid, PnL, health bar, and SPX in the navbar will **not** update until Socket.IO connects or you refresh the page.

System Health **SPX 7527** and navbar **SPX 7526.88** can differ slightly if one path updated and the other did not.

#### Common causes of stuck **Connecting…**

| Cause | Why |
|-------|-----|
| **Socket.IO CDN blocked** | Client library loaded from `cdn.jsdelivr.net`; if blocked/offline, `io()` never initializes |
| **WebSocket upgrade blocked** | Corporate proxy, VPN, or antivirus blocking `ws://` / Engine.IO on port 5002 |
| **No `connect_error` UI** | Failed handshake leaves the label at default **Connecting…** (no timeout → **Disconnected**) |
| **Remote access mismatch** | Server binds `127.0.0.1:5002` only; tunnel/LAN setups sometimes break WebSockets while HTTP still works |
| **Browser tab backgrounded** | Some browsers throttle WebSockets; usually recovers to **Live** when focused |

#### How to verify (operator)

1. Open browser **DevTools → Network → WS** (or Console).
2. Look for Socket.IO requests to `/socket.io/?EIO=4...`
   - **101 Switching Protocols** → should show **Live**
   - **Failed / pending forever** → explains **Connecting…**
3. Hard refresh (`Ctrl+F5`). If still stuck, check CDN/network.
4. Confirm URL is exactly **`http://localhost:5002`** (same host the server binds to).

**Trading impact**: **None.** Streamer, stop_monitor, and `run.py` do not use this WebSocket. Only dashboard auto-refresh is affected.

#### Future improvements (documented, not implemented)

| # | Change |
|---|--------|
| 1 | Handle `connect_error` → show **Disconnected** or **WS failed** |
| 2 | Bundle `socket.io.min.js` locally (no CDN dependency) |
| 3 | Fallback poll `GET /api/summary` every 2s when Socket.IO is down |
| 4 | Merge navbar status with System Health to reduce confusion |

### Bot Status card — **Stopped** vs Launcher green

Easy to misread when you start the bot with **`uv run python run.py`** in a terminal:

| UI element | What it actually checks |
|------------|-------------------------|
| **System Health → Launcher** (green) | `dashboard/bot_status.json` written by **`run.py`** (`state: "running"`) |
| **Bot Status → Stopped** (grey) | Whether **dashboard** spawned its **own** `run.py` via `/api/start_bot` |

Starting `run.py` externally does **not** set `bot_running` inside the dashboard process. Launcher can be green while Bot Status says Stopped — **expected**, not a fault.

Use **Launcher** in System Health (and `launcher.log`) as the source of truth when you launch from the command line.

---

## 2. Logging & Console Noise

### What prints to the terminal

`run.py` starts child processes with `subprocess.Popen` and **no stdout redirect**, so everything shares one console:

| Process | Tag | File log | Console |
|---------|-----|----------|---------|
| `run.py` | `[LAUNCHER]` | `launcher.log` (rewrite each start) | Yes |
| `publish_tastytrade.py` | `[TT-STREAM]` | `stream_pub_tt.log` (rewrite each streamer start) | Yes |
| TastyTrade SDK (DXLink) | `received:`, `received message:` | Same as streamer (via `basicConfig`) | Yes — **main flood** |
| `stop_monitor/run.py` | `[STOP-MON]` | **None** | Yes |

### Why it looks like a lot

The TastyTrade `DXLinkStreamer` logs **every websocket quote batch** at **INFO**. During market hours this can be **many lines per second**, often **5–15 KB per line** (bulk option quotes in COMPACT LIST format).

This is **not** a trading bug — it is verbose SDK logging plus dual `StreamHandler` + `FileHandler` in `publish_tastytrade.py`.

### Disk growth (rough)

| File | Growth rate (market hours) | Reset |
|------|---------------------------|-------|
| `stream_pub_tt.log` | ~10–50 MB/hour possible | Truncated when streamer restarts (`mode='w'`) |
| `launcher.log` | Low (scheduler messages) | Truncated when `run.py` restarts |

### Recommended improvements (future)

| Priority | Change | Benefit |
|----------|--------|---------|
| P1 | Remove `StreamHandler` from streamer; file-only or WARNING+ for SDK | Quiet console, same trading |
| P1 | Add `FileHandler` to `stop_monitor/run.py` | Debug stops without console |
| P2 | Redirect child stdout to per-process log files in `run.py` | Clean operator terminal |
| P2 | Set tastytrade / httpx loggers to WARNING | Stops quote spam in files too |

**Memory note**: Redirecting logs to files does **not** materially increase RAM. Cost is disk, not heap.

---

## 3. Dashboard Stream Log Panel

### Current behavior

- Browser polls `/api/log/stream` every **5 seconds**
- Server `tail_log(path, n=40)`:
  1. Opens log file
  2. **`readlines()` — entire file into memory**
  3. Returns last 40 lines only

Same pattern for Launcher log panel.

### Risk assessment

| Concern | Severity | Notes |
|---------|----------|-------|
| Dashboard RAM spikes | Low–medium | Brief allocation = full file size each poll; painful if log &gt; 100 MB |
| Browser DOM | Low–medium | 40 lines × multi-KB lines can be heavy |
| Trading correctness | **None** | Panel is display-only; MQTT drives prices |

### Recommended improvements (future)

| Option | Description |
|--------|-------------|
| **A. Download link** | Serve `stream_pub_tt.log` / `launcher.log` as static download; remove live tail |
| **B. Health dot only** | Streamer status already uses log mtime — sufficient for “alive?” |
| **C. Efficient tail** | Seek from end of file (read last N KB only), not full `readlines()` |
| **D. Filtered tail** | Show only subscription changes, errors, startup — not every quote batch |

**Recommendation for production**: **B + A** — health dots for live status, download when debugging.

---

## 4. Why SPX Option Quotes Appear at 8:54 Before the 11:00 Tranche

### The confusion

> “First tranche is at 11:00 CT. It’s 8:54. Why is the streamer logging dozens of `.SPXW260622C…` / `.SPXW260622P…` quotes?”

### Short answer

The streamer **does not wait for tranche time**. It starts at **8:30 AM CT** and immediately subscribes to **every symbol listed in `streaming/optsymbols.json`**, plus `SPX`. At 8:54, those symbols are almost certainly **left over from earlier runs** (integration tests, manual tranches, prior-day scanning) — not from today’s 11:00 entry.

### Timeline on a normal day

```
08:30 CT   run.py starts streamer (after wait if before 8:30)
           └─► publish_tastytrade.py reads optsymbols.json
           └─► Subscribes ALL symbols in file + SPX
           └─► DXLink pushes quotes → MQTT + stream_pub_tt.log

08:54 CT   (your observation) Bulk quote tables in log
           └─► Symbols from optsymbols.json, NOT from 11:00 tranche

11:00 CT   First tranche window opens
           └─► app_main.py → vertical_thin → open_spread_tt
           └─► Adds MORE symbols (see below)

15:00 CT   Streamer + stop_monitor stop (launcher)
15:30 CT   Session cleanup (`morning` rules vs `eod` — see PREMARKET_CLEANUP.md)
```

### How symbols get into `optsymbols.json`

| When | Who | What gets added |
|------|-----|-----------------|
| **Prior session / integration test** | `update_options_symbols()` | Leg symbols from test spreads — **persists until 3 PM reset** |
| **Tranche entry scan** | `open_spread_tt.get_open_spread_price_tt()` | Large **candidate grid** around SPX (see below) |
| **After order placed** | `register_spread_symbols()` in `vertical_thin.py` | Short + long leg of placed spread |
| **Stop monitor load** | `monitor.py` on init | Spread legs re-registered if needed |

**Append-only design**: `update_options_symbols()` **extends** the list and deduplicates with `set()`. Symbols are **never removed** when a trade closes — only cleared at **3:00 PM** by the streamer.

### Evidence from Jun 22 launch

`streaming/optsymbols.json` contained **74 symbols** at launch, including OCC-format entries like:

```
SPXW  260622P07450000
SPXW  260622C07555000
...
```

These map to streamer symbols such as `.SPXW260622P7450`, `.SPXW260622C7555` — matching the log lines you saw.

**Source**: Prior integration/off-hours testing (e.g. Jun 21 history trades under `meic0dte/trades/history/`), not the 11:00 scheduler.

### What happens at 11:00 (when tranche actually runs)

Entry does **not** subscribe to two strikes only. It runs a **credit scan**:

From `meic0dte/app/config.py`:

| Parameter | Value | Effect |
|-----------|-------|--------|
| `OTM_MIN` / `OTM_MAX` | 5 / 150 (step 5) | Scan OTM distance from SPX |
| `SPREAD_WIDTH_MIN` / `MAX` | 25 / 35 (step 5) | Spread widths 25, 30 |
| `STEP` | 5 | Strike increment |

`open_spread_tt._scan_symbol_list()` builds **short + long** symbols for every OTM × spread-width combination, then `register_symbols_and_wait()` adds them all to `optsymbols.json` so MQTT mids exist before picking a spread.

**Rough scale per tranche side (Put or Call)**:

- OTM values: 5, 10, …, 145 → **29 steps**
- Spread widths: 25, 30 → **2 widths**
- **~58 strikes × 2 legs ≈ 116 symbols** added per side per tranche attempt

So even on a “clean” day with empty `optsymbols.json`, you will see **large quote batches** as soon as the **first tranche scans** — that is by design for MQTT-based credit selection.

### Why the log shows “tables” of many symbols per line

1. **DXLink COMPACT LIST format** — one websocket message can carry **many** option quotes batched together.
2. **TastyTrade SDK** logs the raw message at INFO (`received:` / `received message:`).
3. Your log line is one batch update for all subscribed symbols that ticked in that interval — not “the bot opened 50 positions.”

### What is actually *needed* at 8:54?

| Symbol set | Needed at 8:54? | Why |
|------------|-----------------|-----|
| `SPX` | **Yes** | Index price for rounding strike grid, dashboard, entry |
| Stale test symbols (74 in file) | **No** | Harmless but wastes bandwidth, log space, DXLink subscription slots |
| Full scan grid (~116/side) | **Not until tranche** | Only added when `get_open_spread_price_tt` runs |

### Operator action (no code change)

Before a clean production day, optionally reset symbol file manually:

```json
{"SYMBOLS": []}
```

Or rely on the **3:00 PM** automatic reset in `publish_tastytrade.py`. If you restart mid-day after tests, stale symbols persist until reset.

---

## 5. Production Launch Checklist (Jun 22)

Verified before / during first live run:

| Check | Result |
|-------|--------|
| `check-env` | `BROKER=tastytrade`, `PAPER_MODE=false` |
| `check-auth` | Session OK |
| `check-mqtt` | Mosquitto localhost:1883 |
| `pytest` | 48 passed |
| `QUANTITY` | 1 |
| `trades/active/` | Empty at start |
| Pending broker test orders | **Operator must cancel manually** |

### Known display issues (fixed in code, restart required)

- Streamer health grey → wrong log path
- SPX `–` in dashboard → wrong MQTT topic prefix
- Live PnL per row → option symbol lookup missing `TASTYTRADE/` prefix

---

## 6. Path to Perfection — Prioritized Backlog

Grouped by theme. Cross-reference GAP IDs where they exist.

### P0 — Operator clarity (no trading logic change)

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 1 | Document `optsymbols.json` lifecycle in README | S | This doc + README section |
| 2 | Pre-market manual reset of `optsymbols.json` or startup clear | S | Avoids stale quotes before first tranche |
| 3 | Restart dashboard after MQTT/topic fixes | S | One-time per deploy |

### P1 — Observability

| # | Item | Effort | GAP |
|---|------|--------|-----|
| 4 | Quiet streamer logging (SDK INFO → WARNING) | S | — |
| 5 | `stop_monitor` file logging | S | — |
| 6 | Replace dashboard stream tail with download + health dot | M | — |
| 7 | Efficient `tail_log` (seek from EOF) | S | — |
| 8 | Launcher health check + restart streamer on crash | M | GAP-09 |
| 9 | MQTT price staleness alert (no tick in 60s) | M | GAP_ANALYSIS §streamer |

### P1 — Symbol subscription hygiene

| # | Item | Effort | GAP |
|---|------|--------|-----|
| 10 | Clear `optsymbols.json` on `run.py` morning start (not only 3 PM) | S | **Done** — see [PREMARKET_CLEANUP.md](PREMARKET_CLEANUP.md) |
| 11 | Remove closed-trade symbols from subscription set | M | V2 plan |
| 12 | Lazy scan: register candidates in smaller batches | L | Reduces quote flood at tranche |

### P2 — Dashboard polish

| # | Item | Effort | Notes |
|---|------|--------|-------|
| 13 | Show `active_trades` count next to StopMon | S | Already in heartbeat JSON |
| 14 | Label `close_mechanism` on closed rows | S | GAP-12 |
| 15 | Distinguish exchange stop vs software breach threshold in UI | M | GAP-05 / GAP-12 |
| 16 | Fix **Connecting…** UX (connect_error, local socket.io, REST fallback poll) | M | See §1 Navbar |
| 17 | Rename **Bot Status** or derive from `bot_status.json` when external `run.py` | S | See §1 Bot Status card |

### P2 — Already resolved (reference)

| GAP | Topic | Status |
|-----|-------|--------|
| GAP-05 | Breach formula | CONFIRMED — CCS example in GAP_ANALYSIS |
| GAP-21 | optsymbols 3 PM reset to `[]` | FIXED |
| GAP-22 | Hybrid threading | FIXED |
| Dashboard TT path | MQTT prefix + stream log | FIXED Jun 22 |

---

## 7. Architecture Reminder — Data Flow

```
run.py (launcher)
  ├── dashboard/server.py     ← reads trades/active, MQTT, heartbeat.json, logs
  ├── publish_tastytrade.py   ← reads optsymbols.json, publishes TASTYTRADE/* MQTT
  ├── stop_monitor/run.py     ← one thread per active trade JSON
  └── meic0dte/app_main.py    ← at tranche windows only

optsymbols.json  ──► streamer subscribe set (persistent, append-only)
MQTT broker      ──► stop_monitor + dashboard live prices
trades/active/   ──► stop_monitor state + dashboard grid
heartbeat.json   ──► dashboard StopMon #N
```

**Key insight**: Tranche schedule controls **when orders are placed**, not **when the streamer runs**. The streamer runs from **8:30–15:00** and mirrors whatever is in `optsymbols.json` from the moment it starts.

---

## 8. Open Questions

1. **Should morning startup clear `optsymbols.json` automatically?**  
   Pro: clean 8:30 session. Con: if launcher restarts mid-day, would drop symbols for open positions until stop_monitor re-registers.

2. **Should entry scan register ~116 symbols at once?**  
   Works today; alternative is progressive scan with smaller MQTT batches (V2 modular plan).

3. **Should console be silent by default with `--verbose` flag?**  
   Better operator UX for systemd / Windows service installs.

---

*Last updated: Jun 22, 2026 — first production launch observations; navbar Connecting… and Bot Status card documented.*
