# Live Session Notes — Jul 21, 2026

**Status:** Morning **11-00 / 12-00 / 12-30 missed** (pre-fix). **Entry monitor fix deployed ~12:23 CT** — **01-15 and 01-45 tranches fired successfully** after restart. Dashboard SPX still ~10pt below broker (documented fix plan below).

**Related:** [LIVE_SESSION_2026-07-13.md](LIVE_SESSION_2026-07-13.md), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md), [LIVE_SESSION_2026-07-20.md](LIVE_SESSION_2026-07-20.md)

---

## Afternoon health check (~13:54 CT)

| Component | Status | Evidence |
|-----------|--------|----------|
| **Entry monitor** | **Healthy** | `trades/entry_monitor_health.json` — tick_count **4408**, last_tick_duration **1.1 ms**, pending_meic **8**, active_workers **0** |
| **01-15 tranche** | **Entered** | Session CSV `state=entered`; `01-15_P/C_20260721T1314*.json` in `trades/active/MEIC_IC/` |
| **01-45 tranche** | **Entered** | Session CSV `state=entered`; `01-45_P/C_20260721T1344*.json` in `trades/active/MEIC_IC/` |
| **Stop monitor V3** | **Healthy** | `trades/heartbeat.json` — 4 active trades, loop_count 17470, ts 13:54:42 |
| **Streamer** | **Live** (restarted once) | `trades/streamer_health.json` — status `live`, SPX tick 13:54:37 |
| **REST gate** | **Healthy** | `runtime/trading_gate.json` — `new_risk_latched=false`, probes ok through **01-45** |
| **Morning tranches** | **Missed (expected)** | 11-00, 12-00, 12-30 still `pending` — windows passed before fix + restart |
| **02-00 tranche** | **Pending** | Next slot 13:59–14:05 CT |

**Minor warnings observed (non-blocking):**

- At restart (~12:39): `TRANCHE_MISSED` CRITICAL for 11-00 / 12-00 / 12-30 — **correct** catch-up from entry coordinator (miss detection now works).
- ~13:48: `STOP_MONITOR MQTT — cache health file absent` — transient after streamer restart; stop_monitor still healthy via heartbeat.
- ~13:52: `STREAMER exited unexpectedly (code 1) — restarting` — launcher auto-restarted streamer; health file shows live again by 13:54.

**Conclusion:** Entry path is working post-fix. Afternoon MEIC exposure is on. Morning slots remain missed (no auto catch-up by design).

---

## Fix deployed — entry monitor background coordinator

### Problem (morning)

Pre-tranche REST probes succeeded (background `ProbeCoordinator`), but **no entry workers spawned** and **no `TRANCHE_MISSED`** logged during or after the 11-00 window. The launcher main loop called `EntryMonitorRunner.tick()` **synchronously** every 5s. If `tick()` blocked (CSV I/O, lock contention, OneDrive), the entire loop stalled. The stall watchdog updated its timer **before** `tick()`, so hangs inside `tick()` were invisible — same failure shape as Jul 13.

### Solution (implemented ~12:23 CT)

Mirrors the probe-coordinator pattern from [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md):

| Change | File | What it does |
|--------|------|--------------|
| **Background entry thread** | `common/entry_coordinator.py` (new) | Runs `EntryMonitorRunner.tick()` every **1s** on dedicated thread; main loop only supervises subprocesses |
| **Stall detection** | `common/entry_coordinator.py` + `run.py` | `ENTRY_MONITOR_STALL` / `ENTRY_MONITOR_TICK_SLOW` CRITICAL if tick gap >30s or single tick >10s |
| **Health file** | `trades/entry_monitor_health.json` | Last tick epoch, duration, pending count, active workers |
| **Heartbeat log** | `common/entry_coordinator.py` | `ENTRY_MONITOR heartbeat` every 60s in launcher log file |
| **CSV lock hardening** | `blocks/entry/runner.py` | `mark_row_entering()` moved **outside** runner lock so file I/O cannot block other tick work |
| **Main-loop watchdog** | `run.py` | `last_tick_mono` updated **after** loop body (measures real iteration gap) |

### Env vars (optional)

| Variable | Default | Purpose |
|----------|---------|---------|
| `ENTRY_MONITOR_COORDINATOR_ENABLED` | `true` | Set `false` to fall back to synchronous tick on main loop |
| `ENTRY_MONITOR_TICK_INTERVAL_SEC` | `1.0` | Entry poll interval |
| `ENTRY_MONITOR_STALL_WARN_SEC` | `30` | Stall alert threshold |
| `ENTRY_MONITOR_TICK_SLOW_SEC` | `10` | Slow-tick alert |
| `ENTRY_MONITOR_HEARTBEAT_LOG_SEC` | `60` | Heartbeat log interval |

### How to verify after restart

1. Launcher log: `Entry monitor coordinator started interval_sec=1.0`
2. Within 60s: `ENTRY_MONITOR heartbeat ticks=... pending_meic=...`
3. Before each tranche: pre-tranche probe ok in `trading_gate.json`
4. In window: `Spawned entry worker for <lot>_P (MEIC_IC)` then `_C` ~2s later
5. `trades/entry_monitor_health.json` `last_tick_epoch` updating every ~1s

---

## Day summary — tranche outcomes

| Tranche | Window (CT) | Outcome | Notes |
|---------|-------------|---------|-------|
| 11-00 | 10:59–11:05 | **Missed** | Pre-fix; main-thread stall |
| 12-00 | 11:59–12:05 | **Missed** | Pre-fix |
| 12-30 | 12:29–12:35 | **Missed** | Restart at 12:39 was after window |
| **01-15** | 13:14–13:20 | **Entered** | Post-fix ✓ |
| **01-45** | 13:44–13:50 | **Entered** | Post-fix ✓ |
| 02-00 | 13:59–14:05 | pending | — |

---

## Morning incident — 11-00 miss (pre-fix)

### Evidence

| Check | Result |
|-------|--------|
| `trades/session/MEIC_IC_2026-07-21.csv` rows `11-00_*` | Still `pending` |
| Launcher log `Spawned entry worker` | **Absent** before restart |
| Launcher log `TRANCHE_MISSED` | **Absent** before restart (tick never completed) |
| `probes_by_tranche.11-00` | `ok=true` at 10:58:31 |
| Streamer during window | **Live** |

### Ruled out

Pause/skip, REST gate latch, failed probes, broker cooldown, Avast SSL (fixed Jul 20).

---

## Dashboard SPX — why it lags broker, and how to fix

### Current behavior

| Layer | Source | What dashboard shows |
|-------|--------|----------------------|
| **Navbar SPX** | MQTT `TASTYTRADE/SPX` | DXLink **quote bid/ask mid** |
| **Broker UI** | TastyTrade REST | **last / mark** price |
| **MEIC entry** | REST at fire time | Correct for orders (not affected by navbar) |

At ~10:48 CT: MQTT quote mid **~7493** vs REST last/mark **~7503.5** (~**10 points**). Feed was **not frozen** — updates every 1–2s, but only `dxlink_quote` events (no `dxlink_trade` for SPX).

### Root cause

1. **SPX excluded from Trade channel subscribe** — `common/market_watch.py` defines `SPX_NO_VOLUME`; `dxlink_trade_symbols()` omits SPX so the streamer never subscribes to SPX trades. Quote-only mids can lag last/mark during fast markets.
2. **Dashboard uses raw MQTT scalar** — `dashboard/server.py` stores `float(msg.payload)` on `TASTYTRADE/SPX` with no REST fallback and no trade-vs-quote preference.
3. **Navbar timestamp** — shows summary build time, not SPX tick age (Bot Status card has tick age).

Streamer already handles SPX trades if they arrive (`streaming/publish_tastytrade.py` lines 263–270) — the subscribe set is the gap.

### Recommended fix (priority order)

> **REST budget:** Pre-tranche probes, entry strike scans, stop reconcile, and fills already consume TastyTrade REST. Default limiter is ~**1 req/s** per process (`TT_REST_MAX_PER_SEC`). **Do not add periodic dashboard REST polling** unless MQTT is unavailable — it would add ~240–360 calls/session at 15s intervals and competes with entry/stop traffic.

#### P1 — Subscribe SPX to DXLink Trade channel (streamer, ~5 lines) — **preferred, zero REST**

**File:** `streaming/ladder_subscribe.py` → `build_trade_subscribe_set()`

Add SPX to the trade subscribe set (handler already exists):

```python
def build_trade_subscribe_set(quote_set: Set[str]) -> Set[str]:
    trades = set(dxlink_trade_symbols())
    trades.add(dxlink_quote_symbol('SPX'))  # last-trade updates for index navbar
    ...
```

`SPX_NO_VOLUME` stays for market_data OHLCV (no volume column) — that flag should **not** block Trade subscribe.

**REST impact:** **None.** DXLink streaming only; same path as VIX/QQQ trade updates.

**Expected result:** MQTT `TASTYTRADE/SPX` updates from `dxlink_trade` (last sale) track broker last/mark much more closely.

#### P2 — Dashboard REST refresh for navbar SPX — **optional fallback only; adds REST load**

**File:** `dashboard/server.py`

Only consider if P1 still leaves a visible gap. If used at all:

- Poll **only when MQTT SPX is stale** (e.g. no tick >30s), not on a fixed 10–15s timer.
- Or **manual only** — “Refresh SPX” button on dashboard (operator-initiated, bypasses cooldown like existing probe button).
- Never run in the launcher or stop_monitor process.

**REST impact:** A fixed 15s poll ≈ **4 calls/min** (~250/session) — low per-call cost but steady background load on a shared account limit. Risk of competing with pre-tranche probes and entry `fetch_spx_price_api` at tranche open.

**Recommendation:** **Skip P2** unless P1 is insufficient after a live session test.

#### P3 — UX: show source + age in navbar

- Label: `SPX 7503.5 (REST, 3s ago)` vs `(quote, 1s ago)`.
- Replace navbar clock with SPX tick age when displaying index price.

#### P4 — Ladder anchor (optional)

`streaming/spx_ladder_symbols.json` uses same MQTT SPX — after P1/P2, ladder strike refresh aligns better with broker.

### What not to change

- **MEIC entry strike selection** — already uses REST at fire time via `spread_scan.py`; navbar fix is display-only.
- **Do not** remove quote subscribe — keep quotes for bid/ask context; layer trade/REST on top.

### Verification after SPX fix

1. Streamer log: SPX in Trade subscribe set count.
2. MQTT sample: both `dxlink_quote` and `dxlink_trade` meta for SPX (or price tracks REST within ~1–2 pts).
3. Dashboard navbar within **1–2 pts** of TastyTrade app last/mark during RTH.

---

## Impact

| Area | Impact |
|------|--------|
| Morning MEIC (11-00, 12-00, 12-30) | **Missed** — no exposure |
| Afternoon MEIC (01-15, 01-45) | **On** — 4 legs active, stops running |
| Entry monitor | **Fixed** — background coordinator + stall detection |
| Dashboard SPX | Still misleading vs broker; fix plan above (not yet implemented) |

---

## Follow-up

| Priority | Item | Status |
|----------|------|--------|
| **P0** | Entry monitor background coordinator | **DONE** (Jul 21 ~12:23) |
| **P1** | SPX Trade-channel subscribe in streamer (no REST) | **DONE** (Jul 21) — restart streamer/launcher to apply |
| **P2** | Dashboard REST SPX refresh | **DEFER** — throttle risk; only if P1 insufficient |
| **P2** | Navbar SPX source + tick age | **OPEN** |
| **P3** | Investigate streamer exit code 1 at 13:52 | **OPEN** (auto-recovered) |

---

*Updated: Jul 21, 2026 ~13:55 CT. Morning miss pre-fix; 01-15 / 01-45 confirmed post-fix.*
