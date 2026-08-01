# Live Session Notes — Jun 26, 2026

**Status:** Operator log + incident notes. **Restart launcher + dashboard** after deploy for all fixes below.  
**Related:** [ENTRY_MONITOR_CSV_OWNERSHIP.md](ENTRY_MONITOR_CSV_OWNERSHIP.md), [STALE_PENDING_TRADE_JSON.md](STALE_PENDING_TRADE_JSON.md) (Change 2)

---

## Incident — Stop× 3 selected but exchange stop placed at 2× (ms-50)

### What the operator saw

Manual spread **ms-50**: Stop× **3** on Place Order. JSON showed `stop_multiplier: 3`, `two_x_short: 3.95`, `two_x_net_credit: 1.95`. TastyTrade stop **479179809** was at **2.45** (2× math). Dashboard Phase column still said **`2× Short Stop`** (display-only).

### Root cause

| Layer | Behavior |
|-------|----------|
| Session / JSON | Stop× saved correctly via `apply_stop_snapshot()` |
| **Exchange stop** | `setup_initial_stop()` ignored JSON — used hardcoded `app_config.STOP_PRCNT_P` (= **2.0**) |
| Fill sync | `_recompute_stop_fields()` also hardcoded `× 2.0` (could clobber thresholds on partial fills) |

For ms-50 short fill **1.32**:

| Multiplier | Formula | Stop price |
|------------|---------|------------|
| **2× (bug)** | `(1.32 − 0.10) × 2` | **2.45** ← what Tasty got |
| **3× (intended)** | `(1.32 − 0.10) × 3` | **3.70** (SPX $0.10 tick above $3) |

### Fix shipped

New module **`blocks/stop/stop_math.py`**:

- `stop_multiplier_for_state(state)` — reads `stop_multiplier` / `plan.stop_multiplier`, falls back to legacy `STOP_PRCNT_*` for old JSON
- `exchange_stop_price(short_fill, multiplier)` — `((fill − 0.10) × mult)` + SPX tick round
- `apply_two_x_thresholds(state, multiplier)` — shared `two_x_short` / `two_x_net_credit` math

Wired into:

| File | Change |
|------|--------|
| `blocks/stop/monitor.py` | `setup_initial_stop(stop_multiplier=None)` uses state multiplier; stop_history reason `initial_short_stop_{N}x` |
| `blocks/stop/fill_sync.py` | `_recompute_stop_fields()` uses state multiplier |
| `blocks/entry/handoff.py` | Persists `stop_multiplier` on JSON; uses shared threshold helper |

Tests: `tests/test_stop_multiplier.py`.

**Note:** ms-50 was **removed from active JSON** — operator closing manually on Tasty. Next manual/MEIC trade with Stop× 3 will place the correct stop after launcher restart.

**Still open (display):** ~~Phase column `2× Short Stop` label~~ — **fixed** (see Follow-up §1).

---

## Incident — Dashboard tables stale until hard refresh (MEIC + Manual)

### What the operator saw

When **MEIC tranches fill** (entry monitor / launcher) or **manual spreads** are placed, the **Today** grid and **Active Manual Spreads** table stay empty or outdated until a **full browser refresh** (F5).

### Root cause

| Layer | Issue |
|-------|--------|
| **WebSocket push** | `_push_loop` ran in a raw `threading.Thread` started at **import time** (before `socketio.run()`). Emits often failed silently (`except: pass`). |
| **Cross-process** | Entry monitor writes CSV/JSON in `run.py`; dashboard is a **separate process** — no direct `notify_update()` on MEIC fill. Client depended entirely on broken socket push. |
| **Manual place** | Dashboard API calls `refreshSummary()` after place, but trades opened by **entry worker** (async) need polling until JSON exists. |

### Fix shipped

**Server (`dashboard/server.py`):**

- `emit_summary_update()` — single entry point for pushes
- Push loop via **`socketio.start_background_task()`** (started on first client `connect`, not raw thread)
- Immediate push on socket connect

**Client (`index.html`):**

- **HTTP poll fallback** — `refreshSummary()` every **3s** on Today tab (works even if Socket.IO fails)

**Phase labels (same deploy):** `PHASE_DISPLAY` → `Short Stop` / `Net Credit Stop` (no hardcoded `2×`).

Tests: `tests/test_dashboard_phase_display.py`.

**Operator:** Restart **dashboard** process (`python dashboard/server.py`) — not just launcher.

---

## Incident — ms-50 Kill Selected (Jun 26 ~11:35 CT)

### What the operator saw

1. Dashboard **Kill** on **ms-50** (7290/7265 PCS, qty 3).
2. Trade JSON moved to `status: "closing"`, `close_mechanism: "manual_close"`.
3. Stop **479179809** cancelled (`spread_close_cancel:manual_close` in stop_history).
4. One **spread debit close** placed: `spread_close_order_id: "479183693"`.
5. Shortly after: many **SELL_TO_CLOSE on the long leg (7265)** — TastyTrade **rejected** them because the **short leg (7290) was still open** (spread not closed yet).
6. Dashboard still showed ms-44–ms-49 ghost rows (see below).

### Root cause (code)

Change 2 added `replace_with_spread_close()` for Kill Selected, but the **`closing` poll loop still runs the legacy leg-by-leg long chase** from the exchange-stop path.

In `blocks/stop/monitor.py` `_poll_once()` when `status == 'closing'`:

| Step | What happened for ms-50 |
|------|-------------------------|
| Kill | `replace_with_spread_close()` cancels stop, places spread close **479183693**, sets `status=closing`. Does **not** set `short_closed_at` (that field is only set when the **short leg** is already closed). |
| Every ~3s poll | `_poll_spread_close()` returned **False** while order was still **working** (only returned True on fill). |
| Fallback | `close_started = self.state.get('short_closed_at', 0)` — missing key → **0** → `now - 0` ≫ 30s delay → **long chase started immediately**. |
| Long chase | `_threaded_long_chase()` → `_place_long_close_limit()` on **7265 only** → broker rejects (short still open). Chase retries → **order spam**. |

Same class of bug in `_recover_closing_on_load()`: if `short_closed_at` is missing it **fabricates** a timestamp and schedules long chase — wrong when `spread_close_order_id` is set.

**Design intent (Change 2):** Kill Selected / Kill All → **one vertical spread close**, never naked long-leg closes. Leg-by-leg + 30s delay remains only for **exchange stop filled** / phase-3 paths (short already closed).

### ms-50 JSON snapshot (after kill)

| Field | Value |
|-------|--------|
| `status` | `closing` |
| `close_mechanism` | `manual_close` |
| `spread_close_order_id` | `479183693` |
| `short_closed_at` | *(absent — triggers bug)* |
| `long_close_order_id` | `null` |

### Fix shipped (same session)

`blocks/stop/monitor.py`:

1. **`_poll_spread_close()`** — return True while spread close is **working** (blocks long chase); on **cancelled/rejected**, clear id and **retry** spread close for `manual_close` / `admin_killswitch`.
2. **`_poll_once()` closing branch** — do not default `short_closed_at` to `0`; skip long chase when `short_closed_at` is unset.
3. **`_recover_closing_on_load()`** — if `spread_close_order_id` set, poll spread close only.

**Operator actions for ms-50:**

1. **Restart launcher** so stop monitor loads the fix (stops long-leg spam).
2. On TastyTrade: check spread close **479183693** — working, filled, or cancelled?
3. Cancel any stray **7265-only** working orders manually.
4. If spread is still open: close via **one vertical debit spread** (7290/7265) in Tasty or re-kill from dashboard after restart.
5. **Do not run full `pytest`** during live session until tests are isolated (see ghost trades).

Regression test: `tests/test_spread_kill.py::test_working_spread_close_does_not_chase_long_leg`.

---

## Ghost trades ms-44–ms-49 (pytest pollution)

### Why they keep reappearing

Dashboard **Active Manual Spreads** reads **`trades/active/MANUAL_SPREAD/*.json`**, not the session CSV. Session CSV correctly has **ms-50 only**, but five JSON files existed:

| File | Lot | Signature |
|------|-----|-----------|
| ms-44, ms-45 | 11:20:47 | order `999`, 7000/6975 |
| ms-48, ms-49 | 11:21:58 | same |
| ms-50 | live | 7290/7265, real orders |

**Source:** `tests/test_manual_session.py` uses a **temp dir for session CSV** but `run_manual_entry_row()` wrote trade JSON to **`state_mod.manual_spread_active_dir()`** (real `trades/active/MANUAL_SPREAD/`). Each pytest run consumed lots from `trades/manual_counter.json` and left artifacts on disk.

**Fix:** Tests now patch `manual_spread_active_dir` → temp dir. Ghost files **deleted again** (ms-44, 45, 48, 49). **Avoid pytest during live trading** until launcher is on fixed code.

---

## ms-50 (removed — manual close on Tasty)

Operator removed ms-50 from the system (`active` JSON deleted, session CSV cleared) and is handling **7290/7265 × 3 PCS** manually on TastyTrade.

Reference (for reconciliation):

| Field | Value |
|-------|--------|
| Entry order | 479179716 |
| Stop order | 479179809 (was 2.45 — 2× bug; intended 3.70 at 3×) |
| Spread close (kill attempt) | 479183693 |

---

## ~~Active manual spread~~ (superseded)

## Cleanup performed (Jun 26)

### Removed from dashboard (test artifacts)

- Deleted active JSON: **ms-44, ms-45, ms-48, ms-49** (pytest mocks — order `999`, 7000/6975, fake +$100 P&L).
- **Not** broker positions; safe file delete only.

### Session CSV trimmed

- Removed stale rows **ms-39, ms-40, ms-41** (earlier test/manual attempts; JSON already in history).
- **Kept only ms-50_P** in `MANUAL_SPREAD_2026-06-26.csv`.

Refresh dashboard → Active Manual Spreads should show **ms-50 only**.

---

## MEIC 11:00 tranche (prod incident → fix shipped)

| Leg | Strikes | Issue | Resolution |
|-----|---------|-------|------------|
| PCS | 7330/7305 | Dashboard blank; CSV stuck `entering` | Manual CSV patch + **Entry Monitor CSV ownership** implemented |
| CCS | 7420/7445 | Showed correctly | C worker’s CSV save overwrote P row (race) |

See [ENTRY_MONITOR_CSV_OWNERSHIP.md](ENTRY_MONITOR_CSV_OWNERSHIP.md) for design + implementation.

---

## Follow-up backlog

### 1. Phase column labels — **shipped**

Renamed `PHASE_DISPLAY` in `dashboard/server.py`:

| Phase | Was | Now |
|-------|-----|-----|
| 1 | `2× Short Stop` | **`Short Stop`** |
| 2 | `2× Net Credit Stop` | **`Net Credit Stop`** |
| 3 | `SPX Proximity Close` | unchanged |

Stop× remains in session plan / Place Order columns — not encoded in phase name.

---

### 2. `stop_profile` string in JSON still says `meic_2x_short` — **shipped**

ms-50 JSON had `"stop_profile": "meic_2x_short"` while `stop_multiplier: 3` lived on entry/plan. Profile name implied a fixed 2×; actual stop× is on JSON / session CSV.

**Fix shipped:**

| Layer | Change |
|-------|--------|
| Profile registry | Canonical name **`meic_credit_spread`**; legacy **`meic_2x_short`** kept as alias for old JSON |
| New trade JSON | `blocks/stop/state.py` writes `stop_profile: meic_credit_spread` |
| `config/strategies.yaml` | Both strategies use `meic_credit_spread` |
| Dashboard Phase column | Shows multiplier from trade, e.g. **`Short Stop (3×)`** via `stop_multiplier_for_state()` |

Tests: `tests/test_stop_profile.py`, `tests/test_dashboard_phase_display.py`.

---

### 3. Manual spread Place Order UX (shipped today)

- Removed Width / Cr Max / duplicate chase fields from §3 Place Order.
- Chase: single “If unfilled → Reprice same spread” with floor + max reprices.
- Table contrast fixes for Active Manual Spreads.
- Immediate summary refresh after place.

---

### 4. Entry Monitor CSV ownership (shipped today)

Workers return `EntryWorkerResult`; `blocks/session/csv_update.py` applies row updates under lock with reload-from-disk. Prevents P+C same-tranche overwrite.

**Operator:** Restart launcher after deploy so running process picks up changes.

---

## Incident — Exit column showed order prices, not Tasty fills (11-00 PCS breach)

### What the operator saw

**7330/7305 PCS** (`11-00_P`) breached via exchange stop. Dashboard **Exit** showed **3.10 (6.20–3.10)** → **−$150** P&L. TastyTrade actual fills:

| Leg | Order (stop/limit) | Actual fill |
|-----|-------------------|-------------|
| Short 7330 BTC | 6.2 / **6.3** limit | **5.7** debit |
| Long 7305 STC | **3.1** limit | **3.3** credit |

True net exit debit: **5.7 − 3.3 = 2.40** → P&L **(1.60 − 2.40) × 100 = −$80** (not −$150).

Slippage **helped** by **+$0.80/sp** (lower exit debit vs order prices).

### Root cause

| Layer | Bug |
|-------|-----|
| **Stop fill** | `handle_stop_order_update()` set `short_close_price` from **stop trigger (6.2)**, not broker fill |
| **Long fill** | `_order_result_from_placed_order()` only parsed **open** legs; **SELL_TO_CLOSE** fell back to **order limit (3.1)** |
| **JSON** | `11-00_P_20260626T105904.json` stored 6.2 / 3.1 |

### Fix shipped

| File | Change |
|------|--------|
| `brokers/tastytrade_broker.py` | Parse **BUY_TO_CLOSE** / **SELL_TO_CLOSE** leg fills via `_leg_avg_fill_price()` |
| `blocks/stop/monitor.py` | On stop fill, `get_order_status()` → actual **short_close_price**; store **short_close_limit_price** |
| `blocks/stop/close_fills.py` | Slippage: `short_close_slippage`, `long_close_slippage`, `exit_slippage` (+ = helped) |
| `dashboard/server.py` + grid | New **Exit Slip** column (`+0.80/sp` format) |
| Trade JSON | **11-00_P** patched with operator fills (5.7 / 3.3) |

Tests: `tests/test_tastytrade_leg_actions.py`, `tests/test_close_fills.py`.

**Slippage sign:** positive **exit_slippage** = exit debit was **lower** than order prices (favorable on a credit spread stop-out).

**Stop Slip column (added):** designated stop trigger (`designated_stop_price` from stop× math) vs **short BTC fill**. Positive = filled better than designated (paid less debit). **Software breach** exception: slippage is the fixed policy uplift (`SOFTWARE_BREACH_SLIPPAGE_UPLIFT` = **$1.00** above designated), **not** fill vs the broker limit order placed at market.

| Close path | Stop Slip basis |
|------------|-----------------|
| Exchange stop | `designated_stop − short_fill` (e.g. 6.2 − 5.7 = **+0.50/sp**) |
| Software breach | **−$1.00/sp** (policy cost vs designated, ignores broker limit) |

**Exit Slip column:** net exit order prices vs actual fills on both legs (unchanged).

---

## Part 2 — Afternoon fixes (same session, not in Part 1 above)

### Manual Place Order duplicated (ms-60 → 2 orders + 2 stops)

**Symptom:** Single dashboard **Place Trade** sent **two** Tasty spread orders and **two** stops ~4s apart (`142355` / `142359` JSON).

**Root cause:** `/api/manual_spread/place` **and** `EntryMonitorRunner` both called `run_manual_entry_row` for the same CSV row (`state=entering`, no `trade_path`).

**Fix shipped:**

| File | Change |
|------|--------|
| `blocks/session/manual_helpers.py` | `dispatch_manual_place()` — launcher active → CSV only; dashboard-only → claim row then inline worker |
| `blocks/entry/runner.py` | `try_claim_manual_row()` before spawning manual worker |
| Lazy import of `run_manual_entry_row` in thread (avoid circular import on startup) |

Tests: `tests/test_manual_place_dispatch.py`, `tests/test_manual_entry_claim.py`.

**Operator:** Delete orphan `ms-60_P_20260626T142355.json` or `142359.json` (keep one); reconcile qty on Tasty.

---

### Post-3:00 PM Tasty rejections (0DTE stop spam)

**Symptom:** Burst of Tasty rejections after **3 PM CT** on 0DTE session — many open trades had `active_stop: null` → stop monitor retried `setup_initial_stop()` every ~3s.

**Root cause:** Launcher `finally` waited until 3:30 for EOD while stop monitor kept running; expired/null stops on open 0DTE JSON triggered placement loops.

**Fix shipped:**

| File | Change |
|------|--------|
| `common/market_hours.py` | `trade_past_0dte_close()` — 0DTE only after 3 PM CT |
| `blocks/stop/monitor.py` | `_broker_actions_frozen()` — no broker actions after 3 PM for 0DTE |
| `blocks/stop/fill_sync.py` | Treat `expired` stop like cancelled for `stop_is_current` |
| `run.py` | Stop monitor + streamer terminate **immediately** at 3 PM |

Tests: `tests/test_market_hours.py`, `tests/test_stop_monitor_0dte_freeze.py`.

**Note:** EOD archive at 15:30 still preferred; **morning cleanup** also archives stale 0DTE — not prioritizing immediate EOD on Ctrl+C.

---

### 01-15 CCS stop display / JSON clobber

**Symptom:** Dashboard showed old stop **479243590** after manual broker fix to **479286130**.

**Root cause:** Stop monitor saved in-memory state every ~5s, overwriting manual JSON edits.

**Fix shipped:** `_reconcile_active_stop_with_broker()`, `_maybe_merge_disk_stop_state()`, `_resolve_active_stop()` fallback in dashboard.

**Operator:** Restart `run.py` after deploy.

---

### Grid text contrast (dark / breached rows)

Low-contrast QTY, phase, stop #, spread columns on dark row backgrounds.

**Fix:** `dashboard/templates/index.html` — light text on `.grid-cell` / `.grid-dim`.

---

### Slippage column — dollars not `/sp`

**Fix:** `slippage_label()` shows total dollars (`+$80.00`); P&L already scaled by qty × 100.

Tests: `tests/test_trade_pnl_qty.py`, `tests/test_close_fills.py`.

---

### 12-00 PCS — software spread breach did not fire

**Symptom:** 7340/7315 PCS closed via **exchange stop** at 6.30; spread at close ~**5.30** (above 2× credit threshold **4.00**). No `breach_cancel` in `stop_history`.

**Design (V2):** Software breach uses **2× net credit + $0.20** on spread mid (not phased short×2 like legacy). Exchange stop on short leg is separate backstop.

**Likely cause (session):** Missing MQTT leg prices or stale streamer — breach check skipped silently; exchange stop still worked at broker.

**Fix shipped — diagnostics (not threshold change):**

| File | Change |
|------|--------|
| `blocks/stop/breach_watch.py` | Snapshot → JSON `breach_watch`; rate-limited logs |
| `blocks/stop/monitor.py` | Refresh watch each poll; persist when stale |
| `blocks/stop/run.py` | Append log to `meic0dte/logs/stop_monitor.log` |
| Dashboard | **SW Breach** column; Stop Monitor Log panel |

Tests: `tests/test_breach_watch.py`, `tests/test_spread_breach_threshold.py`.

**Next session:** Watch SW Breach column + stop monitor log for `missing MQTT` / `streamer stale` vs `Breach watch … gap`.

---

## Operator follow-ups (non-code)

| Item | Action |
|------|--------|
| ms-60 duplicate JSON | Delete one of two active files; confirm single Tasty position |
| Jun 26 `trades/active/` stale files | Launcher interrupted 15:19 before 15:30 EOD — **morning cleanup** will archive; or run cleanup manually |
| 01-15_C / 11-00_C still `open` on disk | Reconcile vs Tasty; archive if flat |
| Restart after deploy | `run.py` + dashboard hard refresh |

---

## Open / deferred (no code this session)

| Item | Notes |
|------|--------|
| EOD on early Ctrl+C | Deferred — morning `run_session_cleanup('morning')` handles stale active JSON |
| 0DTE JSON limbo after 3 PM | Open trades may show `expired`/`null` stop until archive; broker actions frozen |
| Software breach root cause | Awaiting evidence from SW Breach + `stop_monitor.log` on next live day |

---

## Quick reference — where things live

| What | Where |
|------|--------|
| Order IDs | Trade JSON (`open_order_id`, `active_stop.order_id`) |
| Stop× / plan | Session CSV + JSON `stop_multiplier` / `plan` |
| Exchange stop price | `blocks/stop/stop_math.py` → `setup_initial_stop()` in `monitor.py` |
| Dashboard phase text | `PHASE_DISPLAY` + stop× suffix in `dashboard/server.py` |
| Live table refresh | Socket.IO `update` + 3s `/api/summary` poll (`index.html`) |
| Exit fills / slippage | JSON `short_close_price`, `long_close_price`, `exit_slippage`; `blocks/stop/close_fills.py` |
| Active MS trades | `trades/active/MANUAL_SPREAD/*.json` |
| SW breach watch | JSON `breach_watch`; dashboard **SW Breach** column; `meic0dte/logs/stop_monitor.log` |
| Stop profile name | `meic_credit_spread` (legacy alias `meic_2x_short` on disk OK) |
