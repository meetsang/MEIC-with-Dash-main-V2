# Manual Spread Strategy — Design Document

**Date**: Jun 23, 2026 (revised)  
**Status**: Implemented (Jun 23, 2026)  
**Audience**: Operator (volatile-day workflow) and future implementation  
**Related**: [DASHBOARD_IMPLEMENTATION.md](DASHBOARD_IMPLEMENTATION.md), [OPERATIONAL_HARDENING.md](OPERATIONAL_HARDENING.md), [V2_MODULAR_REWRITE.md](V2_MODULAR_REWRITE.md)

---

## Purpose

On high-volatility days, the operator may want to:

1. **Disable all scheduled MEIC tranche entries** from the start of the session.
2. **Trade directionally** — enter **one side only** (PCS or CCS), not the paired IC.
3. **Search for spreads around a target credit** from a dedicated dashboard tab and **Place Trade**.
4. **Manage working entry orders** (modify limit credit via cancel/replace until filled).
5. **Reuse the existing stop lifecycle** once filled — exchange stop, software breach, phase upgrades, long chase — without modifying MEIC scheduling code.

This document evaluates today’s **Pause Selected** behavior, confirms what keeps running, and specifies the **Manual Spread Strategy** as a parallel strategy with its own folder tree, dashboard tab, and controls.

---

## Part 1: What Happens If You “Pause All” From the Start

There is **no dedicated “Pause All” button** today. The equivalent workflow is:

1. Start `run.py` (launcher + streamer + stop_monitor).
2. Open the dashboard **MEIC** tab tranche grid.
3. Check **Select All**.
4. Click **Pause Selected** (only acts on slots in `pending` state).

That writes all 12 slot keys to `meic0dte/trades/pause_tranches.json`:

```json
{
  "paused_slots": ["11-00_C", "11-00_P", "12-00_C", "12-00_P", "..."],
  "ts": "09:00:00 CT"
}
```

### 1.1 What does NOT fire

| Component | Behavior when all slots paused |
|-----------|--------------------------------|
| **Scheduled MEIC tranches** | Skipped — `run.py` never calls `run_tranche()` for that window |
| **MEIC entry threads** | Never started — `app_main.py` / `vertical_thin.tranche()` not invoked |
| **MEIC credit scan grid** | Not run at tranche time — no bulk symbol registration from MEIC entry |

Pause is **MEIC entry-only**. It does not stop background services.

### 1.2 What KEEPS running

| Component | Behavior |
|-----------|----------|
| **Launcher (`run.py`)** | Main loop continues until 3:00 PM CT |
| **Streamer (`publish_tastytrade.py`)** | Keeps publishing SPX + subscribed option mids to MQTT |
| **Stop monitor (`stop_monitor/runner.py`)** | Supervisor loop; one `StopMonitor` thread per active trade JSON |
| **Dashboard** | Both tabs (MEIC + Manual Spread) |
| **Token refresh** | Unaffected |

So: **nothing MEIC-scheduled enters**, but **streamer and stop loops continue normally**. Manual Spread trades written to `manual_spread/trades/active/` are monitored the same way as MEIC once filled.

### 1.3 Pause semantics — important details

**Both sides must be paused for a tranche lot to skip.**

```51:58:MEIC-with-Dash-main/run.py
def _is_tranche_paused(lot: str) -> bool:
    """Return True if both sides of a lot are paused via the dashboard pause file."""
    ...
    return f'{lot}_C' in paused and f'{lot}_P' in paused
```

| Scenario | Result |
|----------|--------|
| All 12 slots paused before first window | All 6 tranches skipped for the day |
| Only PCS paused for `11-00` | **CCS still enters** at 11:00 |
| Pause on slot already `open` | **No-op** — pause only affects `pending` slots |
| Unpause one side later | Tranche still fires the unpaused side when its window hits |

**Recommendation**: Pause **both** C and P for every lot, or use a future **“Pause All MEIC”** button (MEIC tab only) that pauses all 12 slots in one click.

### 1.4 Pause vs Kill vs Stop Bot

| Control | Scope | Effect |
|---------|-------|--------|
| **Pause Selected** | MEIC tab only | Skips future scheduled MEIC entries |
| **Kill All / Kill Selected** | MEIC tab only | Force-closes MEIC trades; Kill Selected also pauses pending MEIC slots |
| **Kill Selected** | Manual Spread tab | Force-closes selected manual spread trades only |
| **Stop Bot** | Global | Terminates launcher — streamer and stop_monitor exit; exchange stops remain but software breach stops |

For manual-only days: **Pause All MEIC** + keep bot running. Do **not** use Stop Bot unless you intentionally drop software monitoring.

### 1.5 Session timeline (manual-day mode)

```
08:30 CT   Streamer starts (run.py)
08:31 CT   MEIC tab → Pause All (all 12 slots)
           └─► pause_tranches.json populated
09:00–14:00 Manual Spread tab → scan → pick spread → place working order
           └─► writes manual_spread/trades/active/MANUAL_SPREAD_*.json
           └─► stop_monitor watches pending_fill → places stop only after fill
15:00 CT   Launcher shuts down streamer + stop_monitor (normal EOD)
```

---

## Part 2: Manual Spread Strategy — Goals and Non-Goals

### Goals

- **Dedicated dashboard tab** (“Manual Spread”) separate from the MEIC tranche grid.
- **Operator-driven entry form** with sensible defaults:

  | Field | Default | Notes |
  |-------|---------|-------|
  | Underlying | SPX | v1: SPX / SPXW only |
  | Expiry | Today’s 0DTE | Date picker; default = session expiry |
  | Side | PCS | Toggle Put / Call |
  | Spread width | 25 | Points between short and long strike |
  | Target credit | 0.60 | Credit the operator wants to collect |
  | Quantity | 1 | User-entered stepper |

- **Scan around target credit**: Given the inputs above, return **~3 candidate spreads** near the target credit for the chosen expiry — not a fixed MEIC min/max band. Example output for target **$0.60**, width **25**, PCS:

  | # | Strikes | Market credit |
  |---|---------|---------------|
  | 1 | 7525 / 7500 | $0.65 |
  | 2 | 7520 / 7495 | $0.50 |
  | 3 | 7530 / 7505 | $1.00 |

  Rank by distance from target credit (closest first).

- **Select, adjust, place**: Operator picks one row, may **edit limit credit** (e.g. change $0.65 → **$0.75** to seek more premium and accept a longer wait), sets quantity, clicks **Place Trade**.

- **Working-order management**: Treat as an **options spread entry screen**:
  - Order may rest unfilled — no abort on “credit drift.”
  - While `status == pending_fill`, show **Modify Price** (cancel working order + place new order at updated limit) and **Cancel Order**.
  - **Stop logic waits for fill** — `StopMonitor` already syncs fills from `open_order_id` and only places exchange stops once `filled_quantity > 0` (same as MEIC thin tranche).

- **Same stop mechanism as MEIC** once filled (phases 1–3, breach, long chase, 3 PM admin close).

- **Zero impact** on MEIC tranche scheduling, `vertical_thin.py`, or `run.py` entry loop.

- **Multiple concurrent manual spreads** allowed — all visible on the Manual Spread tab grid with live PnL, phase, stop, per-row kill.

- **Strategy-specific controls**: MEIC tab buttons (Pause, Kill, etc.) affect MEIC only. Manual Spread tab buttons affect manual trades only.

### Non-Goals (v1)

- No automatic directional signals.
- No paired IC / hedged MEIC replacement.
- No changes to MEIC credit targets, tranche times, or VIX gates.
- No separate stop_monitor **process** — extend the existing supervisor to watch both trade directories.
- No debit spreads or structures beyond vertical credit spreads.
- No multi-underlying beyond SPX (field present for future; v1 enforces SPX).

---

## Part 3: Architecture — Reuse, Don’t Fork

```
MEIC (unchanged):
  run.py → app_main → vertical_thin → open_spread_tt → meic0dte/trades/active/*.json
                                              ↓
                                    stop_monitor (watches both dirs)

Manual Spread (new):
  dashboard Manual tab → manual_spread/entry.py → spread_scan + broker
                                              ↓
                         manual_spread/trades/active/*.json
                                              ↓
                                    stop_monitor (same threads, same phases)
```

**Key insight**: `StopMonitor` does **not** branch on `entry.strategy`. It reads status, legs, phases, and MQTT prices. `strategy: "MANUAL_SPREAD"` is metadata for dashboard and history only.

### Shared modules

| Module | Reuse |
|--------|-------|
| `spread_scan.py` (new, extracted) | Credit scan — MEIC uses first match; Manual Spread uses target-centered top 3 |
| `open_spread_tt.write_pending_trade_state()` | Handshake JSON after order place (strategy param) |
| `stop_monitor/fill_sync.py` | Working-order fill sync |
| `stop_monitor/monitor.py` + `phases.py` | Post-fill lifecycle |
| `common/strike_guard.py` | **Same legacy rule** as MEIC — scan both active dirs; block only short↔long flip on same side |
| Dashboard close/kill | Same command-file pattern, scoped by filename / tab |

CLI reference: `tests/adhoc_integration.py place-trade` — same handshake pattern, different strategy path.

---

## Part 4: Trade Identity and Folder Layout

### 4.1 Strategy tag and naming

| Field | MEIC | Manual Spread |
|-------|------|---------------|
| `entry.strategy` | `MEIC_IC` | `MANUAL_SPREAD` |
| Filename prefix | `MEIC_IC_SPX_...` | `MANUAL_SPREAD_SPX_...` |
| Lot label | `11-00`, `12-00`, … | `ms-1`, `ms-2`, … (sequential) |

### 4.2 Parallel folder tree (mirrors MEIC)

Manual Spread lives in its **own top-level folder**, not under `meic0dte/`:

```
manual_spread/
  trades/
    active/              ← stop_monitor watches (alongside MEIC)
    history/             ← closed trades archived here
    manual_counter.json  ← lot sequence { "next": 1 }
  entry.py               ← preview, place, modify, cancel orchestration
  config.py              ← defaults: width, target credit, scan count, etc.

meic0dte/                ← unchanged MEIC tree
  trades/
    active/
    history/
    pause_tranches.json  ← MEIC pause only
```

**Config additions** (`common/tt_config.py` or env):

```
MANUAL_SPREAD_ACTIVE_DIR=manual_spread/trades/active
MANUAL_SPREAD_CLOSED_DIR=manual_spread/trades/history
```

**Stop monitor change** (small): `MonitorRunner` scans **both** `meic0dte/trades/active/*.json` and `manual_spread/trades/active/*.json`. Same thread model per file.

### 4.3 Dashboard — separate tab

Two strategy sub-tabs under **Today** (see **Part 14** for full screen draft and interactive mockup):

| Tab | Contents |
|-----|----------|
| **MEIC** | Existing tranche grid, MEIC-only Pause / Kill / Unpause / Kill All / **Pause All MEIC** |
| **Manual Spread** | Entry form + candidate list + active manual grid + Manual-only Kill Selected |

`build_summary()` returns `{ meic_grid: [...], manual_trades: [...] }`. MEIC grid matching logic unchanged.

#### Manual Spread tab — summary layout

Four stacked sections: **(1) Scan parameters** → **(2) Candidates** → **(3) Place order** → **(4) Active grid**. Full wireframe, field behavior, colors, and click path are in **Part 14**.

| State | Meaning | Actions |
|-------|---------|---------|
| `working` | Order placed, `pending_fill`, qty not fully filled | Modify Price, Cancel Order |
| `open` | Filled; stop_monitor active | Kill Selected |
| `closing` / `closed` | Same as MEIC | Read-only / history |

---

## Part 5: API Design

Routes under `/api/manual_spread/` (broker calls in dashboard backend only).

### 5.1 `POST /api/manual_spread/scan`

**Request:**

```json
{
  "underlying": "SPX",
  "expiry": "2026-06-23",
  "side": "P",
  "spread_width": 25,
  "target_credit": 0.60,
  "max_results": 3
}
```

**Behavior:**

1. Validate inputs; require fresh MQTT SPX (< 30s).
2. Register symbols for scan band around SPX (width fixed, OTM sweep).
3. For each valid strike pair at `spread_width`, compute market credit from MQTT mids.
4. Return top `max_results` sorted by `abs(market_credit - target_credit)`.
5. Run `leg_overlap_conflict()` against **all open/pending JSONs in both active dirs**. Flag only **legacy flip conflicts** (see §5.7) — warn on scan, block on place.

**Response:**

```json
{
  "status": "ok",
  "spx": 7520,
  "target_credit": 0.60,
  "candidates": [
    {
      "rank": 1,
      "short_strike": 7525,
      "long_strike": 7500,
      "market_credit": 0.65,
      "distance_from_target": 0.05,
      "short_mid": 1.20,
      "long_mid": 0.55,
      "overlap_warning": null
    }
  ],
  "scan_ms": 8500
}
```

Show spinner during scan (5–15s typical). Debounce Scan button.

### 5.2 `POST /api/manual_spread/place`

**Request:**

```json
{
  "underlying": "SPX",
  "expiry": "2026-06-23",
  "side": "P",
  "short_strike": 7525,
  "long_strike": 7500,
  "limit_credit": 0.75,
  "quantity": 1
}
```

**Behavior:**

1. Overlap guard — **block** only if `leg_overlap_conflict()` returns a reason (§5.7). Scan both MEIC and Manual Spread active dirs; **same rule** as today’s MEIC entry scan.
2. `broker.place_spread_order(...)` at `limit_credit` (limit order, not market).
3. `write_pending_trade_state(strategy='MANUAL_SPREAD', lot='ms-N', ...)` → `manual_spread/trades/active/`.
4. `register_spread_symbols(...)`.
5. Return immediately — **do not** block on fill in the HTTP request. Dashboard polls trade JSON for fill progress.
6. `StopMonitor` picks up file; `_sync_entry_fills()` until filled; then places stop.

**No credit-drift abort** — operator chose a limit that may rest. Higher limit = willing to wait.

### 5.3 `POST /api/manual_spread/modify`

For working orders (`pending_fill`, not fully filled):

**Request:**

```json
{
  "filename": "MANUAL_SPREAD_SPX_20260623_ms-1_103045_P.json",
  "new_limit_credit": 0.70
}
```

**Behavior:**

1. Cancel existing open order via broker.
2. Place new spread order at `new_limit_credit` (same strikes/qty minus already-filled qty if partial).
3. Update JSON: new `open_order_id`, reset/sync open_order section.
4. If partially filled, only replace remaining quantity.

### 5.4 `POST /api/manual_spread/cancel`

Cancel working entry order; set trade status to `cancelled` and archive or delete JSON (no stop_monitor thread needed).

### 5.5 `POST /api/manual_spread/close`

Same as existing `POST /api/close_trade` — writes `{filename}.close.json` command; stop_monitor runs breach pipeline. Manual tab passes manual-spread filenames only.

### 5.6 Safety gates

| Gate | Action |
|------|--------|
| MQTT SPX stale (> 30s) | Block scan/place |
| Leg flip conflict (§5.7) | **Block** place/modify — same rule as legacy MEIC `check_long_short` |
| Different expiry / symbol | Implicit — different OCC symbol → no conflict |
| MEIC slots not paused | Warn on Manual tab banner; do not block |
| After 2:45 PM CT | Warn — phase 3 proximity may activate soon after fill |
| Duplicate place in flight | UI mutex per row |

### 5.7 Strike guard — legacy long/short flip rule (not broad overlap)

**Design correction:** Manual Spread does **not** add a blanket “same underlying + expiry” block. It reuses the **exact painful-overlap rule** from legacy MEIC — the one that prevents opening a spread whose leg would flip an existing position on the same option contract.

#### What legacy MEIC checked (`MEIC-main`)

In `meic0dte/open/spreadprice.py`, `check_long_short()` reads open lots from `order_params.json` and **skips** a candidate spread when (same side P or C only):

| New spread leg | Existing spread leg | Block? |
|----------------|---------------------|--------|
| **Long** strike | Already someone’s **short** | **Yes** — would buy to open what you’re short elsewhere |
| **Short** strike | Already someone’s **long** | **Yes** — would sell to open against existing long protection |

```103:111:MEIC-main/meic0dte/open/spreadprice.py
    for orderlot, options in data.items():
        for option_type, details in options.items():
            if opt_type == option_type:
                if long_symbol == details["short_symbol"]:
                    ...
                    return True
                if short_symbol == details["long_symbol"]:
                    ...
                    return True
    return False
```

Comment in scan loop: *“Check if the long symbol is already shorted as part of another lot.”*

#### What TastyTrade port already does (`MEIC-with-Dash-main`)

`common/strike_guard.py` → `leg_overlap_conflict()` — same two checks, against `trades/active/*.json` instead of `order_params.json`:

```50:59:MEIC-with-Dash-main/common/strike_guard.py
        if symbols_equivalent(new_long, ex_short):
            return (
                f'long {new_long} already open as short leg in lot {lot} '
                f'({path})'
            )
        if symbols_equivalent(new_short, ex_long):
            return (
                f'short {new_short} already open as long leg in lot {lot} '
                f'({path})'
            )
```

MEIC entry already calls this during credit scan (`open_spread_tt.py`) — **skip candidate**, try next strike. Production log example:

```
Strike overlap — skip: long .SPXW260622P7440 already open as short leg in lot 11-00
```

#### What is **allowed** (not blocked)

| Scenario | Allowed? |
|----------|----------|
| Two spreads **short the same strike** (different long wings) | **Yes** — legacy never checked this |
| Two spreads **long the same strike** | **Yes** |
| Overlapping strike ranges (e.g. 7525/7500 and 7520/7495) with no shared leg flip | **Yes** |
| Same strike number, **call vs put** (7635C + 7635P) | **Yes** — puts and calls are independent |
| Manual + MEIC spreads with no leg flip | **Yes** |
| Different expiry (different OCC symbol) | **Yes** — implicitly different contract |

#### Manual Spread implementation

| Where | Behavior |
|-------|----------|
| **Scan** | Run `leg_overlap_conflict()` per candidate; set `overlap_warning` if blocked; dim row but still show (operator sees why) |
| **Place / modify** | **Hard block** with legacy message if conflict |
| **Dirs scanned** | Extend glob to **both** `meic0dte/trades/active/` and `manual_spread/trades/active/` — **logic unchanged** |
| **MEIC entry** | No behavior change — only widen scan path when dual-dir helper lands |

#### Worked examples (same 0DTE, both PCS)

**Existing:** MEIC lot short **7525** / long **7500** (open).

| New candidate | Block? | Why |
|---------------|--------|-----|
| Short 7520 / long **7525** | **Yes** | New long 7525 = existing short 7525 |
| Short 7515 / long 7490 | **No** | No leg equals opposite role elsewhere |
| Short **7500** / long 7475 | **Yes** | New short 7500 = existing long 7500 |
| Short 7525 / long 7500 (duplicate structure) | **No** | Same strikes same roles — odd but not a flip conflict |

---

## Part 6: Shared Scan Module (Minimal MEIC Touch)

Extract from `open_spread_tt.get_open_spread_price_tt()`:

```
meic0dte/open/spread_scan.py   (new)
  scan_credit_spreads(
      broker, opt_type, expiry, log, *,
      spread_width,           # single width for manual; range for MEIC
      target_credit=None,     # manual: center ranking
      credit_min, credit_max, # MEIC: band filter
      max_results=1,
  ) → list[SpreadCandidate]
```

| Caller | Parameters |
|--------|------------|
| MEIC `get_open_spread_price_tt` | `credit_min/max` from config, width 25–35, `max_results=1` — **behavior unchanged** |
| Manual Spread scan | `target_credit`, fixed `spread_width`, `max_results=3` |

Only intentional MEIC touch: extract refactor, no logic change.

---

## Part 7: Stop Lifecycle — Confirmed Unchanged After Fill

| Phase | When | Owner |
|-------|------|-------|
| **Working order** | `status: pending_fill` | Dashboard modify/cancel; `fill_sync` in stop_monitor |
| **Stop placement** | First fill on short leg | `_ensure_stop_for_filled_qty()` — **not before fill** |
| Phase 1 | Filled + open | 2× short stop + software breach |
| Phase 2 | Long ≤ $0.05 | 2× net credit stop |
| Phase 3 | ~2:51 PM CT | SPX proximity close |
| Breach / kill | Any time | Same pipeline as MEIC |
| 3 PM admin | Hard close | Same as MEIC |

---

## Part 8: History and Reporting

Manual Spread has **its own history store**, parallel to MEIC:

| Store | Path / change |
|-------|---------------|
| Active trades | `manual_spread/trades/active/` |
| Closed archive | `manual_spread/trades/history/` |
| SQLite | New table `manual_spread_trades` **or** separate `manual_spread.db` — not mixed into MEIC history by default |
| Dashboard History | Manual Spread tab sub-section “Closed Today”; MEIC tab keeps existing history |
| Discord (if enabled) | `[MANUAL_SPREAD]` prefix |

---

## Part 9: File Layout Summary

```
manual_spread/                    NEW package
  config.py
  entry.py                        preview, place, modify, cancel
  trades/active/
  trades/history/
  trades/manual_counter.json

meic0dte/open/spread_scan.py      NEW — shared scan
meic0dte/open/open_spread_tt.py   MODIFY — delegate to spread_scan

stop_monitor/runner.py            MODIFY — watch both active dirs
stop_monitor/state.py             MODIFY — active_path_glob covers both dirs (for strike_guard)
common/strike_guard.py            MODIFY — iterate both active dirs; keep legacy flip logic only

dashboard/
  server.py                       /api/manual_spread/*, dual payload in build_summary
  templates/index.html            Tab bar: MEIC | Manual Spread
  manual_spread_entry.py          thin wrapper (optional)

changes/MANUAL_STRATEGY.md        THIS DOC
```

**Explicitly NOT modified:**

- `run.py` tranche loop (optional `POST /api/pause_all_meic` only)
- `meic0dte/app/vertical_thin.py`
- `stop_monitor/monitor.py` phase logic
- MEIC tranche grid slot matching

---

## Part 10: Operator Workflow (Volatile Day)

1. Start bot + dashboard.
2. **MEIC tab** → Pause All (all 12 slots).
3. Confirm health: Streamer green, StopMon green, SPX live.
4. **Manual Spread tab** → set side, target credit $0.60, width 25 → **Scan**.
5. Pick closest candidate (e.g. 7525/7500 @ $0.65), raise limit to **$0.75** → **Place Trade**.
6. If not filled in desired time → **Modify Price** or **Cancel Order**.
7. Once filled → monitor phase/stop on active grid; **Kill Selected** if needed.
8. Optional: later unpause MEIC slots on MEIC tab if conditions normalize.

---

## Part 11: Implementation Phases

| Phase | Scope | Effort |
|-------|-------|--------|
| **P0** | “Pause All MEIC” button on MEIC tab + tooltip (both-side rule) | Small |
| **P1** | `manual_spread/` folder, config, `spread_scan.py` extract, dual-dir stop_monitor watch | Medium |
| **P2** | `/api/manual_spread/scan` + Manual Spread tab UI (form + candidates) | Medium |
| **P3** | Place / modify / cancel + active grid + kill (strategy-scoped buttons) | Medium |
| **P4** | History DB + closed trades archive + dual-dir strike guard (same legacy rule) | Small |
| **P5** | Fast/narrow scan mode (optional) | Optional |

---

## Part 12: Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Scan registers many symbols | Debounce Scan; show progress; optional narrow OTM band (P5) |
| Leg flip conflict (§5.7) | `leg_overlap_conflict` on both dirs — block only new-long=existing-short or new-short=existing-long (same side) |
| Overlapping strike ranges | **Allowed** — not checked by legacy |
| Same strike, call vs put | **Allowed** |
| Working order never fills | Modify Price + Cancel; no timeout abort in v1 |
| Strategy button confusion | All kill/pause/place controls scoped to active tab only |
| Manual JSON in wrong folder | Entry module writes only under `manual_spread/trades/active/` |
| Stop Bot during manual session | Banner warning on Manual tab |

---

## Part 13: Decisions (formerly open questions)

| Question | Decision |
|----------|----------|
| Quantity | User-entered on entry form (default 1) |
| Credit selection | User sets target → scan returns ~3 → pick row → edit limit credit before place |
| Multiple concurrent spreads | **Allowed** — all shown on Manual Spread active grid |
| Strike guard | **Legacy flip rule only** — block when new long hits an existing short (or new short hits an existing long) on the **same side**; scan MEIC + Manual dirs |
| Different expiry / underlying | **Allowed** — different symbol or no flip conflict |
| Paper mode | Respect `PAPER_MODE` — same as MEIC |
| Strategy name | **Manual Spread Strategy** (`MANUAL_SPREAD` in JSON) |
| Stop before fill | **Never** — stop_monitor waits for fill sync (existing behavior) |

---

## Summary

| Question | Answer |
|----------|--------|
| Pause all MEIC from the start — do tranches fire? | **No**, if all 12 slots (both C and P per lot) are paused |
| Do streamer and stop loops keep running? | **Yes** |
| Same stop mechanism after fill? | **Yes** — shared `StopMonitor` + phases |
| Separate from MEIC? | **Yes** — own folder, tab, history, strategy-scoped buttons |
| Working limit orders? | **Yes** — place, modify (cancel+replace), cancel; stop only after fill |
| Strike overlap? | **Legacy flip only** — block if new long = existing short (or vice versa) on same side; not broad same-expiry block |
| Impact on MEIC code? | **Minimal** — shared scan extract + stop_monitor watches second directory |

**Next step**: Implement P0 + P1 when approved.

---

## Part 14: Screen Draft (UI Specification)

Interactive mockup: [manual-spread-screen-draft.canvas.tsx](/Users/meets/.cursor/projects/c-Users-meets-Downloads-MEIC-SPX/canvases/manual-spread-screen-draft.canvas.tsx) — open beside chat to click through candidate selection and limit credit editing.

### 14.1 Tab structure

Extend the existing **Today | History** navbar with a strategy switcher inside **Today**:

```
┌──────────────────────────────────────────────────────────────────────────┐
│ MEIC Autotrader          SPX 7,520.45    10:42:18 CT    ● Live          │
├──────────────────────────────────────────────────────────────────────────┤
│  Today  |  History                                                       │
│  ┌─────────────┬──────────────────┐                                      │
│  │ ● MEIC      │   Manual Spread  │   ← strategy sub-tabs (Today only)   │
│  └─────────────┴──────────────────┘                                      │
└──────────────────────────────────────────────────────────────────────────┘
```

| Tab | Scope |
|-----|-------|
| **Today → MEIC** | Existing PnL banner, health, MEIC controls, 12-slot tranche grid |
| **Today → Manual Spread** | Entry screen + active manual grid (this spec) |
| **History** | Sub-filter: MEIC / Manual Spread / All |

Global navbar (SPX, connection) stays shared. **Stop Bot** remains on MEIC tab only (or a small global footer) — not duplicated on Manual tab.

### 14.2 Manual Spread tab — full layout

```
┌─ Banner (conditional) ─────────────────────────────────────────────────────┐
│ ⚠ MEIC tranches still active — 8 of 12 slots not paused.  [Pause All MEIC]│
└──────────────────────────────────────────────────────────────────────────┘

┌─ Day PnL (manual only) ─────┐  ┌─ System Health (shared) ────────────────┐
│ Manual Spread P&L  +$55     │  │ ● Launcher  ● Streamer  ● StopMon  SPX │
│ 1 open · 1 working          │  └─────────────────────────────────────────┘
└─────────────────────────────┘

┌─ 1. Scan parameters ─────────────────────────────────────────────────────┐
│ Underlying   Expiry        Side              Width    Target $    Qty      │
│ [ SPX    ▼]  [ Jun 23 ▼]  (•) Put  ( ) Call  [ 25 ]   [ 0.60 ]  [ 1  ]   │
│                                                          [ Scan ]          │
│  Scanning… ████████░░  (8s) — only while request in flight               │
└──────────────────────────────────────────────────────────────────────────┘

┌─ 2. Candidates — pick one ───────────────────────────────────────────────┐
│    Strikes      Mkt $   Δ target   Short mid   Long mid   Warn             │
│ ○  7525 / 7500  0.65    +0.05      1.20        0.55                      │
│ ○  7520 / 7495  0.50    −0.10      0.95        0.45                      │
│ ○  7530 / 7505  1.00    +0.40      1.55        0.55                      │
│  Empty state: "Run Scan to find spreads near $0.60"                        │
└──────────────────────────────────────────────────────────────────────────┘

┌─ 3. Place order ─────────────────────────────────────────────────────────┐
│ Selected: 7525 / 7500 (PCS)                                              │
│ Limit credit  [ 0.75 ]   ← pre-filled from mkt credit; operator may raise │
│ Quantity      [ 1    ]   (mirrors scan qty; editable)                      │
│ [ Place Trade ]   disabled until candidate selected + scan not running     │
└──────────────────────────────────────────────────────────────────────────┘

┌─ 4. Active Manual Spreads ───────────────────────────────────────────────┐
│ [ Modify Price ]  [ Cancel Order ]  [ Kill Selected ]   ← row-context      │
│ ☐  Lot   Side  Strikes    Limit  Fill   Entry  Spread  P&L    Phase  Stop │
│ ☐  ms-2  P    7525/7500   0.75   0/1    —      —       —      —      —   │
│       State: WORKING  (amber)   Order #88421 working 12m                   │
│ ☐  ms-1  P    7410/7385   —      1/1    1.45   1.20    +$25   Ph1    2.90 │
│       State: OPEN  (green)                                                 │
└──────────────────────────────────────────────────────────────────────────┘
```

### 14.3 Field behavior

| Field | Behavior |
|-------|----------|
| Underlying | v1: SPX only (dropdown disabled or single option) |
| Expiry | Defaults to session 0DTE; date picker for weeklies later |
| Side | Radio Put / Call — drives PCS vs CCS label in candidates |
| Width | Integer points; scan uses exact width only (not MEIC 25–35 range) |
| Target credit | Decimal; scan ranks by `\|mkt_credit − target\|` |
| Qty | Integer ≥ 1; passed to place and shown in active grid |
| Limit credit | Initialized to selected row's **market credit**; operator edits before place |
| Scan | POST `/api/manual_spread/scan`; disable form during request |

### 14.4 Row states and actions (active grid)

| State | Badge color | Row actions | Stop monitor |
|-------|-------------|-------------|--------------|
| `working` | Amber | **Modify Price**, **Cancel Order** | `pending_fill` — syncs fill, no stop yet |
| `open` | Green | **Kill Selected** | Full phase lifecycle |
| `closing` | Blue | **Kill Selected** (optional) | Long chase |
| `closed` | Grey | None | Archived to `manual_spread/trades/history/` |

**Modify Price** flow: inline prompt or small modal → new limit → POST `/modify` → row stays `working` with updated limit.

**Cancel Order**: POST `/cancel` → row removed or greyed `cancelled`.

### 14.5 Visual tokens (match existing dashboard)

Reuse `index.html` dark theme:

| Token | Value | Usage |
|-------|-------|-------|
| Page bg | `#0f1117` | Body |
| Card bg | `#1a1d27` | Sections 1–4 |
| Card border | `#2d3148` | All cards |
| Accent / primary btn | `#6366f1` or `#16a34a` | Scan, Place Trade |
| Kill btn | `#dc2626` | Kill Selected |
| Warning btn | `#d97706` | Modify Price |
| Text primary | `#e2e8f0` | Labels |
| Text muted | `#94a3b8` | Column headers |
| Working state | `#eab308` | Amber badge |
| Open state | `#22c55e` | Green badge |

### 14.6 Operator click path (happy path)

```
1. Open Today → Manual Spread
2. Confirm health dots green; optional banner → Pause All MEIC
3. Set Put, width 25, target $0.60 → Scan
4. Select row 7525/7500 ($0.65)
5. Raise limit to $0.75 → Place Trade
6. Row appears as WORKING in grid
7. (Optional) Modify Price to $0.70 if not filling
8. Fill → state OPEN, Phase 1, stop price appears
9. Monitor PnL / phase; Kill Selected if needed
```

### 14.7 MEIC tab changes (P0)

Add to MEIC controls row:

```
[ Pause All MEIC ]   ← new; pauses all 12 slots in one POST
```

Tooltip: "Both Put and Call must be paused per tranche lot for the scheduler to skip it."
