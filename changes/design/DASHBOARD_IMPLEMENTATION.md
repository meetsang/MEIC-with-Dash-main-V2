# Dashboard Implementation Plan

**Date**: Jun 21, 2026
**Status**: Planning
**Predecessor**: [GAP_ANALYSIS.md](GAP_ANALYSIS.md) — all 17 FIX items implemented and tested.

---

## Current State

The dashboard (`dashboard/server.py`) is Flask + Flask-SocketIO on port 5002. It works but reads the **legacy** `order_params.json` (Schwab-era shared file). The TastyTrade architecture uses **per-trade JSON** under `meic0dte/trades/active/`. This means the dashboard currently shows nothing useful for TastyTrade trades.

| Component | Current | Target |
|-----------|---------|--------|
| Trade data source | `order_params.json` (legacy) | `meic0dte/trades/active/*.json` |
| Live prices | MQTT `live_prices` dict | Same (already works) |
| PnL calculation | Server-side, 2s push | Same approach, correct data source |
| Kill switch | MQTT publish (Schwab path) | Direct process signal + broker cancel |
| Thread health | None | Heartbeat counters via shared state |
| Per-leg close | None | MQTT command → stop_monitor |
| History | SQLite `meic_trades.db` | Same |

> **Q: Is SQLite already integrated?** Yes. Python's standard library includes `sqlite3` — no pip install needed. `dashboard/db.py` already uses it and creates `meic_trades.db` at runtime via `init_db()` on first import.

---

## Features

### Feature 1: Live PnL from per-trade JSON + MQTT prices

**Data source**: Read `meic0dte/trades/active/*.json` files every push cycle (2s).

**How it works today vs. target**:

```
TODAY (broken for TastyTrade):
  order_params.json → build_summary() → SocketIO push

TARGET:
  trades/active/*.json → build_summary_tt() → SocketIO push
  MQTT live_prices{}  ────────────────────┘
```

**What the UI shows per open trade**:

| Column | Source |
|--------|--------|
| Lot | `state.lot` |
| Side | `state.entry.side` (P/C) |
| Time opened | `state.entry.timestamp` |
| Short strike | `state.short_leg.strike` |
| Long strike | `state.long_leg.strike` |
| Entry credit | `state.entry.net_credit` |
| Current short | `live_prices[short_leg.symbol]` from MQTT |
| Current long | `live_prices[long_leg.symbol]` from MQTT |
| Current spread | `current_short - current_long` |
| Live PnL | `(entry_credit - current_spread) × 100 × quantity` |

> **Sign convention confirmed**: Entry credit 1.50, current spread 1.00 → `(1.50 - 1.00) × 100 = +$50 profit` (green). Spread widens to 2.00 → `(1.50 - 2.00) × 100 = -$50 loss` (red). The formula inherently gives positive = profit, negative = loss for credit spreads. Will add explicit test cases during implementation to verify green/red display matches.
| Status | `state.status` (open / closing / closed) |
| Stop phase | `state.phases` — which phase is active |
| Stop price | `state.active_stop.stop_price` |

**Aggregate banner**: Sum of all open live PnL + all closed PnL for the day.

**File locking concern**: We do NOT lock files. `build_summary_tt()` reads each JSON with a simple `open()` + `json.load()` in a try/except. If stop_monitor is mid-write (atomic via `tempfile` + `os.replace` per `state.py`), the read either gets the old complete file or the new complete file — never a partial write. This is safe on all platforms including Windows because `os.replace` is atomic. The 2s read cycle is negligible I/O.

**Closed trades**: Trades with `status == 'closed'` have `state.close` dict with final PnL fields. These get upserted to SQLite once (existing `_synced_trades` pattern).

---

### Feature 2: Kill All + Stop Program

Two buttons, distinct actions:

**Button A: "Kill All Positions"** (red, with confirmation dialog)
- Sends `POST /api/killswitch`
- Backend: For each `trades/active/*.json` with `status == 'open'`:
  1. Cancel any working stop order via broker
  2. Place market close on short leg, then long leg chase (standard lifecycle)
  3. Set `close_mechanism = 'admin_killswitch'`

**Chosen approach: Force-breach via sentinel file.**
Dashboard writes `killswitch.json`. `stop_monitor` detects it on next poll (~3s) and **forces all active trades through the breach code path** (limit chase on short leg, then GAP-01 long chase). No new close logic needed; the entire breach pipeline is reused as-is. The dashboard stays broker-free.

> **Close mechanism label**: The trade JSON `close_mechanism` will be set to `'manual_close'` (per-trade close) or `'admin_killswitch'` (Kill All) — **not** `'breach'`. The code path is the same (reuse breach pipeline), but the label distinguishes manual vs. market-triggered closes for analytics. This lets you filter/report on "how many trades were manually closed vs. breached" in history.

**Button B: "Stop Bot"** (orange, with confirmation)
- Sends `POST /api/stop_bot`
- Backend: Writes `bot_status.json` with `{"state": "kill", ...}`. The launcher's main loop in `run.py` checks this and exits cleanly (streamer + stop_monitor terminated).
- Positions remain open with exchange stops protecting them.

**Button C: "Pause Further Tranches"** (yellow, toggle)
- Sends `POST /api/pause_tranches`
- Backend: Writes `meic0dte/trades/pause_tranches.json` with `{"paused": true, "ts": "..."}`.
- The tranche scheduler in `run.py` checks for this file before opening new positions. If present and `paused == true`, it skips the scheduled entry but keeps streamer, stop_monitor, and all existing trade monitoring fully active.
- Button toggles: click again to resume (deletes the file or sets `paused: false`).
- Positions already open continue with full stop monitoring and exchange stops.

> **Mix and match**: Button A + B = close all open trades and stop further tranches.

**DECIDED: Full tranche grid with per-row controls.**

All 12 trade slots (6 tranches × 2 sides) are shown from the start. Each tranche's Put and Call sides are **grouped visually** (e.g., shared row background or bordered pair). Each row has a checkbox for bulk selection.

**Controls**:
- Top-level: **Select All** checkbox + **Kill Selected** (red) + **Pause Selected** (yellow) + **Stop Bot** (orange)
- Per-row: Checkbox for selection

**Context-aware behavior**:
- **Kill** on an active/open trade → force-breach close (`close_mechanism = 'manual_close'`)
- **Pause** on a future/pending tranche → skip that tranche's scheduled entry
- **Kill** on a future tranche → same as Pause (nothing to close yet)
- **Pause** on an active trade → no-op (trade already entered, can only Kill)

**State color coding** (per trade slot):

| State | Color | Meaning |
|-------|-------|---------|
| `pending` | Light gray | Scheduled but not yet entered |
| `open` | Green | Active, being monitored |
| `closing` | Blue | Short filled, chasing long leg |
| `closed` | Dark gray | Fully closed (PnL final) |
| `killed` | Red | Manually closed via dashboard |
| `paused` | Yellow/amber | Skipped — will not enter |
| `breached` | Orange | Market breach triggered close |

**Visual layout** (6 tranche groups, 2 rows each):

```
┌─ 11:00 CT ──────────────────────────────────────────────────────────┐
│ ☐ CCS  7635/7660  $1.50 entry  $1.20 live  +$30   ● open   Loop#891│
│ ☐ PCS  7410/7385  $1.40 entry  $1.10 live  +$30   ● open   Loop#445│
├─ 12:00 CT ──────────────────────────────────────────────────────────┤
│ ☐ CCS  —/—        —            —            —      ● pending       │
│ ☐ PCS  —/—        —            —            —      ● pending       │
├─ 12:30 CT ──────────────────────────────────────────────────────────┤
│ ☐ CCS  —/—        —            —            —      ● paused        │
│ ☐ PCS  —/—        —            —            —      ● paused        │
├─ ... remaining tranches ...                                         │
└─────────────────────────────────────────────────────────────────────┘
         [ ☑ Select All ]   [ Kill Selected ]   [ Pause Selected ]
```

---

### Feature 3: Heartbeat / Thread Health Monitor

**Goal**: Show that streamer, stop_monitor, and each trade's monitor thread are alive.

**Data sources** (already exist or easy to add):

| Component | Heartbeat source | How |
|-----------|-----------------|-----|
| Launcher | `bot_status.json` | Already written by `run.py` with timestamp |
| Streamer | `stream_pub_tt.log` last line timestamp | Already exists |
| Stop monitor (supervisor) | New: `stop_monitor/runner.py` writes `meic0dte/trades/heartbeat.json` | Add: `{"ts": ..., "active_trades": 2, "loop_count": 4523}` |
| Per-trade monitor | Already in trade JSON | `state.recovery.last_heartbeat` — updated each poll cycle |
| MQTT | `live_prices` dict age | Dashboard checks if SPX price is stale (> 30s old) |

**UI**: A small status panel (collapsible) showing:

```
┌─ System Health ──────────────────────────────┐
│ Launcher     ● Running    8:30:15 AM         │
│ Streamer     ● Live       MQTT: 142 msg/min  │
│ Stop Monitor ● Active     Loop #4523         │
│ MQTT SPX     ● Fresh      7505.43 (2s ago)   │
│                                              │
│ Trade Monitors:                              │
│  11-00 CCS 7635/7660  ● Loop #891  3s ago   │
│  12-00 PCS 7410/7385  ● Loop #445  3s ago   │
└──────────────────────────────────────────────┘
```

Green dot = heartbeat within 15s. Yellow = 15–60s stale. Red = > 60s or missing.

**Implementation**: `build_summary_tt()` reads the heartbeat JSON and each trade's `recovery.last_heartbeat`. No new threads needed — just file reads on the existing 2s push cycle.

**Layout decision**: The top panel shows only **system-level health** (4 items: Launcher, Streamer, Stop Monitor supervisor, MQTT/SPX freshness). Per-trade heartbeats (loop count, last seen) are shown **inline in each trade's row** in the open trades table — no duplication. With up to 12 trades (6 tranches × 2 sides), repeating them in both the panel and the table would be cluttered.
---

### Feature 4: Kill One Leg / One Tranche

**UI**: Each open trade row gets a "Close" button (with confirmation).

**Mechanism**: Dashboard writes a per-trade command file:

```
meic0dte/trades/commands/{trade_filename}.close.json
```

Content: `{"action": "close", "ts": "...", "source": "dashboard"}`

`stop_monitor/monitor.py` checks for this file at the top of each `_poll_once()` cycle. If found:
1. Cancel active stop
2. Place limit close on short leg at MQTT mid
3. Enter `closing` lifecycle (GAP-01 long chase)
4. Delete the command file

**Why file-based**: No new IPC mechanism needed. Dashboard stays broker-free. stop_monitor already reads files every 3s. The command file is a one-shot trigger — deleted after processing.

**Chosen approach: Force-breach on single trade** — same pattern as Kill All, but scoped to one trade. The command file triggers a forced breach on that specific trade ID. `stop_monitor` picks it up and runs the existing `_threaded_phase_execute()` pipeline for just that trade — limit chase on short, then long leg close. All existing breach handling, parallel threading, and `closing` lifecycle logic is reused. Zero new integration needed.

**Per-leg close** (advanced, phase 2): If user wants to close only the long leg (to let short expire worthless), the command file specifies `{"action": "close_long_only", ...}`. Less common scenario — defer to phase 2.
---

## Resource Constraints (Google Free Tier)

The free tier VM (e2-micro: 0.25 vCPU, 1 GB RAM) is tight. Design principles:

| Constraint | How we handle it |
|------------|-----------------|
| CPU | No polling loops in dashboard — SocketIO push from server, not client pull |
| Memory | No in-memory caching of trade history. SQLite for queries. `live_prices` dict is ~100 entries max |
| Disk I/O | Read 6–12 small JSON files every 2s = negligible. SQLite writes only on close events |
| Network | No external API calls from dashboard. MQTT is localhost only |
| Concurrency | Flask-SocketIO `threading` mode (no eventlet/gevent needed for < 5 clients) |
| Frontend | Single HTML file, CDN for Bootstrap/Chart.js. No SPA framework |

**What NOT to do**:
- No WebSocket to broker from dashboard
- No REST polling from frontend (SocketIO push replaces it)
- No background price fetching threads — MQTT subscriber already handles this
- No heavy charting libraries (Chart.js from CDN is fine)
- No database migrations or ORM

---

## Implementation Order

| Phase | What | Files changed | Effort |
|-------|------|---------------|--------|
| **Phase 1** | Switch data source to `trades/active/*.json` | `dashboard/server.py` | Core change |
| **Phase 2** | Update UI table columns (time, strikes, entry, live, PnL) | `dashboard/templates/index.html` | Template |
| **Phase 3** | Aggregate PnL banner (open + closed) | `server.py` + `index.html` | Small |
| **Phase 4** | Heartbeat panel (read existing timestamps + add runner heartbeat) | `server.py`, `index.html`, `stop_monitor/runner.py` | Medium |
| **Phase 5** | Kill All via force-breach sentinel | `server.py`, `stop_monitor/monitor.py` | Medium |
| **Phase 6** | Stop Bot + Pause Tranches buttons | `server.py`, `run.py`, `index.html` | Small |
| **Phase 7** | Per-trade close via force-breach command | `server.py`, `index.html`, `stop_monitor/monitor.py` | Medium |
| **Phase 8** | Closed trades table + SQLite sync from new JSON | `server.py`, `db.py` | Medium |

Phases 1–3 are the critical path (live PnL visibility). Phases 4–7 are operational controls. Phase 8 is history/reporting.

---

## Data Flow Diagram (Target)

```
                   ┌─────────────────────────────────────┐
                   │         Dashboard (Flask 5002)       │
                   │                                     │
  MQTT prices ────►│  live_prices{} ──┐                  │
  (localhost)      │                  ▼                  │
                   │  trades/active/*.json               │
                   │  ──► build_summary_tt() ──► SocketIO│──► Browser
                   │                                     │
                   │  heartbeat.json ──┘                 │
                   │                                     │
                   │  POST /api/killswitch               │
                   │  ──► writes killswitch.json         │
                   │                                     │
                   │  POST /api/close_trade              │
                   │  ──► writes commands/*.close.json   │
                   │                                     │
                   │  POST /api/stop_bot                 │
                   │  ──► writes bot_status.json kill    │
                   └─────────────────────────────────────┘
                              ▲
                              │ SocketIO 2s push
                              ▼
                   ┌──────────────────────┐
                   │   Browser (1 tab)    │
                   │   - Open trades PnL  │
                   │   - Health panel     │
                   │   - Kill / Close     │
                   │   - History tab      │
                   └──────────────────────┘
```

**Key principle**: Dashboard only reads files and writes command files. It never touches the broker API.

---

## Decisions Resolved

1. **Kill switch** — DECIDED: Sentinel file + force-breach. ~3s latency acceptable. Dashboard stays broker-free.
2. **Kill All / Per-trade close mechanism** — DECIDED: Both reuse existing breach pipeline via forced breach state. No new close logic.
3. **Heartbeat layout** — DECIDED: System health (4 items) at top. Per-trade heartbeat inline in trade rows.
4. **Tranche grid** — DECIDED: Show all 12 slots from the start. Checkboxes + Kill/Pause. Color-coded states. Put/Call grouped per tranche.
5. **Close mechanism labels** — DECIDED: `manual_close` for per-trade, `admin_killswitch` for Kill All. Distinct from `breach`.

## Open Questions

1. **Should the dashboard also show stop phase details?** — e.g., "Phase 2 activated: stop upgraded to 2× net credit". The data is in the JSON (`phases` dict). Low effort to display.

2. **Log viewer** — Keep the existing log tail panel? It polls every 5s. Could reduce to on-demand (click to refresh) to save resources.

3. **Mobile-friendly?** — Bootstrap 5 is responsive out of the box. No extra work needed unless custom layouts are wanted.

---

*Last updated: Jun 21, 2026 — planning phase. No code changes yet.*
