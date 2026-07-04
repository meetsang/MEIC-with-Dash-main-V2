# V2 Operational Changes (Pending)

**Date:** Jun 25, 2026  
**Status:** Documented — fully locked; implementation plan in **V2.5** (`../MEIC-with-Dash-main-V2.5/docs/IMPLEMENTATION.md`)  
**Related:** [PREMARKET_CLEANUP.md](PREMARKET_CLEANUP.md), [MANUAL_STRATEGY.md](MANUAL_STRATEGY.md), [DASHBOARD_IMPLEMENTATION.md](DASHBOARD_IMPLEMENTATION.md)

**Implementation order:** **4a+4b** (one trade JSON, dashboard pick, scan fix) → **4e+4c** (session CSV bootstrap, entry monitor, MEIC) → **4d** (manual session rows) → **4f** (chase tests). **Change 2** (spread kill) in parallel.

### At a glance

| Change | What | Owner after v1 |
|--------|------|----------------|
| **1** | Ghost JSON on MEIC retry; dashboard picks wrong file | One trade JSON + dashboard pick (**4a/4b**) |
| **2** | Kill Selected / Kill All → one spread close | Stop monitor (**breach unchanged**) |
| **3** | Strike column contrast (CSS) | Dashboard |
| **4** | Entry monitor + session CSV + stop handoff | Entry monitor (open) / stop monitor (protect) |

**Data flow (Change 4):** `strategies.yaml` (defaults) → **`trades/session/{strategy}_{date}.csv`** (today’s plan, dashboard-editable) → **`trades/active/…json`** (filled trade, stop monitor).

---

# Change 1 — Stale pending-fill JSON (cancelled entry retries)

## Problem

When a MEIC tranche **cancels an unfilled spread and retries** at a different credit/strike (`meic0dte/app/vertical_thin.py`), the entry thread:

1. Writes a handshake JSON for **attempt 1** (`status: pending_fill`)
2. Cancels the broker order after `FILL_WAIT_MAX` (5s) with no fill
3. Places **attempt 2** and writes a **new** JSON with a new order id

The **first JSON is left in** `trades/active/{strategy}/`. It is never archived or deleted.

### Impact

| Component | Effect |
|-----------|--------|
| **Dashboard** | Two files match the same `lot` + side (e.g. `11-00` + `C`). `build_summary()` uses `matching[0]` (first glob result). The ghost file (lower order id in filename) wins → wrong strikes, entry credit, empty stop, state shown as **Open**. |
| **Stop monitor** | Extra idle thread on `pending_fill` with no broker order; no stop placed (harmless for risk). |
| **Heartbeat** | `active_trades` count inflated. |

### Example (Jun 25, 2026 — 11:00 call)

| File | Order | Strikes | Entry | Status | Real? |
|------|-------|---------|-------|--------|-------|
| `…C_776911.json` | 478776911 | 7420/7445 | $1.50 | `pending_fill` | **No** — cancelled, retry |
| `…C_776988.json` | 478776988 | 7415/7440 | $1.80 | `closed` | **Yes** — filled, stopped, long closed |

Dashboard showed 776911; TastyTrade showed 776988.

### Example (Jun 25, 2026 — 12:30 put, retry blocked)

| Step | Detail |
|------|--------|
| Attempt 1 | 7320/7295 @ $1.60 — order 478832760, no fill, cancelled |
| Ghost file | `…P_832760.json` left as `pending_fill` |
| Attempt 2 | Scan only — overlap / strike guard failure, **no order placed** |
| TastyTrade | No 12:30 put position (correct) |
| Dashboard | Showed ghost put until manual delete |

See **Live session observations** below for full chain (strike guard self-conflict + earlier open puts).

## Manual fix (immediate)

**Yes — deleting the ghost file fixes the dashboard** for that slot (after the next refresh / WebSocket update).

1. Identify stale files in `trades/active/MEIC_IC/` (or `MANUAL_SPREAD/`):
   - `status: pending_fill`
   - `filled_quantity: 0`
   - `open_order.status` still `live` but broker order **cancelled**
   - Often an **older order id** in the filename than the filled trade for the same lot+side

2. Confirm on TastyTrade that the order id in the file is cancelled / never filled.

3. **Delete** the stale JSON from `trades/active/` (do not delete the filled/closed trade file).

4. Refresh dashboard — the grid should show the remaining file for that lot+side.

Optional: if a **closed** trade still sits in `active/` and a copy exists under `trades/history/`, removing the `active/` copy also cleans heartbeat counts (EOD cleanup will archive by expiry anyway).

### Jun 25 cleanup applied

| Removed | Reason |
|---------|--------|
| `…1100_1059_C_776911.json` | 11:00 call attempt 1 — cancelled, retry filled on 776988 |
| `…1230_1229_P_832760.json` | 12:30 put attempt 1 — cancelled, attempt 2 never placed |

---

## Live session observations (Jun 25, 2026 paper day)

Additional findings from a full production-style run (MEIC tranches + manual spread + kills).

### Ghost JSON can block the retry in the same tranche (12:30 put)

Change 1 is not only a **dashboard display** problem. On **12:30 put**:

1. Attempt 1 placed **7320/7295** @ $1.60 (order 478832760), no fill in 5s → cancelled at broker.
2. Handshake `…P_832760.json` left as `pending_fill` in `active/`.
3. Attempt 2 re-scanned at 12:29:13; log shows overlap shift warning and two candidate lines, then **stops** — no `Attempt 2: placing spread`.
4. `open_spread_tt` raised `TerminateRequest` for **strike overlap** (`common/strike_guard.py` treats `pending_fill` as active — same as `open`).
5. The ghost file **conflicts with itself** on retry: new short **7320** equals the ghost spread’s long leg rule (`short already open as long leg in lot 12-30`).
6. **No second put JSON was written** — unlike 11:00 call where attempt 2 filled and created `776988`. Only **one** ghost file existed for 12:30 put.

**Open puts already on the book** at 12:29 made overlap shifts harder:

| Lot | Strikes | Status |
|-----|---------|--------|
| 11-00 | 7315 / 7290 | open |
| 12-00 | 7305 / 7280 | open |

Shifted candidates (e.g. 7325/7300 → 7335/7310) logged `still conflicts or out of credit band` ($0.90–$1.85).

**Call side unaffected** — strike guard is per side (P vs C); 12:30 call filled and got stop 478832815.

### TerminateRequest is easy to miss in logs

When attempt 2 aborts on overlap, `utilities.TerminateRequest` prints `SCHWAB MEIC ERROR: …` to **stdout**, not necessarily to `{lot}_put.log`. The put log can end abruptly after scan lines with no explicit failure reason. Check launcher console or add logging in a future fix.

### Dashboard quirks confirmed

| Symptom | Cause |
|---------|--------|
| Wrong strikes / empty stop # | `matching[0]` picks ghost `pending_fill` (lower order id in filename) |
| `pending_fill` shown as **Open** | `_slot_state_from_trade()` maps `pending_fill` → `open` |
| Closed trade still in grid | Closed JSON still under `active/` until EOD archive removes it; may duplicate `history/` copy |
| Heartbeat `active_trades` too high | Counts ghosts + closed files still in `active/` |

**11:00 call after exchange stop:** Real trade closed correctly on broker (stop 478777011 @ 4.50, long 478803132 @ 1.05, ~34s total). Dashboard showed ghost 776911 until manual delete — not a stop bug.

### One ghost vs two files per lot+side

| Tranche | Side | Ghost from attempt 1 | Filled attempt 2 JSON | Notes |
|---------|------|--------------------|------------------------|-------|
| 11:00 | C | 776911 (7420/7445) | 776988 (7415/7440, later closed) | Two files; dashboard picked ghost |
| 12:30 | P | 832760 (7320/7295) | *(none)* | Attempt 2 never placed; retry blocked by ghost |

When retry **succeeds**, expect **two** JSON files until cleanup. When retry **fails**, expect **one** ghost that still pollutes dashboard and strike guard.

### Closed trades lingering in `active/`

After exchange stop or manual kill, finalized trades were copied to `trades/history/` but often **remained** in `trades/active/` with `status: closed` (e.g. 776988 call). Harmless for trading; inflates stop-monitor thread count and heartbeat. EOD cleanup archives by expiry; optional manual remove from `active/` if a history copy exists.

### What worked (for context)

- Orchestrator fired tranches on schedule (11:00, 12:00, 12:30).
- Entry → fill → 2× short stop → exchange stop → 30s → long close path validated on 11:00 call.
- Manual spread open, stop, and dashboard kill completed (Change 2 concern: leg-by-leg, not failure).
- Streamer health + stale-price freeze operational.
- Morning session cleanup at 8:29 CT; bot `running` all session.

---

## Planned code fix

Long-term owner is **Change 4** (one JSON per trade). Short-term items that can ship in phase **4a+4b** before the full entry monitor:

1. **One JSON per lot+side:** On cancel-and-retry, update the **same file** (new `open_order_id`, append `order_history[]`) — never write a second file. Stable filename: `{lot}_{side}_{entry_ts}.json` under `trades/active/{strategy}/`.
2. **Dashboard (`server.py`):** When multiple trades match a slot, prefer `open` / `closing` / `closed` over `pending_fill`; prefer `filled_quantity > 0`; prefer newest entry timestamp.
3. **Scan pick (`spread_scan.py` / `open_spread_tt.py`):** Return first non-overlap candidate; fix ambiguous overlap-shift logging (credit vs overlap).
4. **Stop runner gate:** Do not spawn a stop thread until `status: open` and full fill (Change 4e). Until then, stop monitor should ignore `pending_fill` JSONs.
5. **Logging:** Write `TerminateRequest` / overlap failures to tranche `{lot}_{put|call}.log`, not stdout only.
6. **Finalize close:** Archive JSON from `active/` when `status: closed` (not only copy to `history/`).

Items (1)–(3) replace the old `vertical_thin.py` delete-on-retry approach once the entry monitor owns chase (Change 4c).

## Acceptance

- Cancel + retry leaves **at most one** active JSON per lot+side
- **Retry attempt is not blocked** by the previous attempt’s ghost handshake
- Dashboard tranche grid matches broker for strikes, stop #, and state
- `pytest` — cancel-retry does not leave `pending_fill` in `active/`; retry succeeds when strikes available

---

# Change 2 — Kill switch / manual close: close whole spread in one order

## Problem

Dashboard **Kill Selected**, **Kill All** (`killswitch.json`), and manual-spread kill all route through the same **leg-by-leg breach pipeline** in `blocks/stop/monitor.py`:

1. Cancel working exchange stop (if any)
2. **Single-leg** `BUY_TO_CLOSE` limit on the **short** only (`replace_with_limit_close`)
3. Wait for short fill → `status: closing`
4. **`LONG_CLOSE_DELAY_SEC` (30s)** pause
5. **Single-leg** `SELL_TO_CLOSE` limit on the **long** (`_chase_long_close`)

This matches the MEIC **exchange stop → long close** design (short stop fills first, then long is chased). It is **not** ideal for operator-initiated kills on manual credit spreads (CCS/PCS), where the goal is to **flatten the entire vertical immediately** with minimal leg risk.

### Example (Jun 25, 2026 — manual CCS kill)

Manual call credit spread `ms-3` (`MANUAL_SPREAD_SPX_260625_ms3_1114_C_787783.json`), dashboard **Kill Selected** (`close_mechanism: manual_close`):

| Step | Time | Broker action |
|------|------|----------------|
| 1 | 11:38:09 | Cancel stop **478787876** |
| 2 | 11:38:09 | Short leg limit BTC **478802814** @ **0.45** (single leg) |
| 3 | 11:38:12 | Short filled |
| 4 | ~30s wait | `LONG_CLOSE_DELAY_SEC` |
| 5 | 11:38:45 | Long leg STC **478803132** @ **0.15** (second order) |

Trade **did** close (`status: closed`, entry $1.30 → exit ~$0.30 debit). Operator concern: **two separate orders + 30s naked long exposure** after short is closed — should be **one spread close** on TastyTrade.

### Current code paths (all leg-by-leg today)

| Trigger | Entry point | Close path |
|---------|-------------|------------|
| Kill All | `POST /api/killswitch` → `killswitch.json` | `StopMonitor.replace_with_limit_close(reason='admin_killswitch')` |
| Kill Selected (MEIC or Manual) | `POST /api/close_trade` → `{filename}.close.json` | Same pipeline (`manual_close`) |
| Software breach | Phase 1 spread mark ≥ threshold | Same pipeline (`software_breach`) |
| Exchange stop fill | Stop order fills at broker | Short filled → 30s → long chase (by design) |

**Note:** Exchange-stop → long chase (last row) should **stay leg-by-leg** — the short already filled at the exchange. Change 2 targets **operator kills** and optionally **admin killswitch**, not normal stop fills.

## Desired behavior

On **manual kill** or **killswitch** for a **credit vertical** (MEIC IC leg or Manual Spread):

1. Cancel any working stop / single-leg close orders on the spread
2. Place **one TastyTrade vertical spread order** to close:
   - Short leg: `BUY_TO_CLOSE`
   - Long leg: `SELL_TO_CLOSE`
   - Limit price = current spread mark (debit to close), with chase/reprice if unfilled
3. On fill → set `status: closed`, record single close order id, archive JSON
4. **No 30s long-only chase** for this path

| Path | Spread close in one order? |
|------|----------------------------|
| **Kill Selected** (MEIC or Manual) | **Yes** — one vertical spread close |
| **Kill All** / killswitch | **Yes** — one vertical spread close per trade |
| Exchange stop filled | **No** — keep 30s + long chase (short already filled at exchange) |
| **Software breach** | **No change** — keep current short-only limit path (out of Change 2 scope) |

## Gaps to implement

1. **Broker:** `BrokerBase.place_spread_close_order(short, long, qty, debit_limit)` — mirror `place_spread_order` but `BUY_TO_CLOSE` + `SELL_TO_CLOSE` (`brokers/tastytrade_broker.py` today only opens spreads).

2. **Stop block:** New close mode e.g. `replace_with_spread_close(reason)` — use for `manual_close` and `admin_killswitch` only. **Do not** change `software_breach` (keeps `replace_with_limit_close` + existing path).

3. **Pricing:** Use MQTT spread mark (`short_mid - long_mid`) for initial debit limit; reuse existing chase/step-down logic at **spread** level.

4. **State JSON:** Record `close_order_id` (spread) vs separate `long_close_order_id` / short leg ids for analytics.

5. **Dashboard:** No API change required — same kill endpoints; behavior change is in stop monitor.

## Acceptance

- Kill Selected / Kill All → **one** spread close order (not leg-by-leg + 30s)
- Software breach path **unchanged** from today
- Exchange stop fill path **unchanged** (30s + long chase)
- `pytest` — mock broker receives single spread-close call for `manual_close` / `admin_killswitch`
- Paper-day: kill manual CCS; confirm no 30s gap with only long leg open

---

## Change log

| Date | Change | Action |
|------|--------|--------|
| Jun 25, 2026 | Change 1 — ghost `776911` JSON (11:00 C) | Manually deleted; dashboard call row fixed |
| Jun 25, 2026 | Change 1 — ghost `832760` JSON (12:30 P) | Manually deleted; 12:30 put retry had already failed |
| Jun 25, 2026 | Change 1 — 12:30 put retry blocked | Documented: ghost + strike guard + open 11:00/12:00 puts |
| Jun 25, 2026 | Change 1 — dashboard / closed-in-active | Documented display quirks and heartbeat inflation |
| Jun 25, 2026 | Change 2 — manual CCS kill (`ms-3`) | Observed leg-by-leg close; spread-close documented |
| Jun 25, 2026 | Paper day — 11:00 C exchange stop | Validated stop → 30s → long close; broker matched JSON |
| Jun 25, 2026 | **Deep dive** — 01-15 P / 02-00 P / ms-8 | See **Incident deep dive (Jun 25 afternoon)** below; script `scripts/investigate_jun25_incidents.py` |
| Jun 25, 2026 | Change 3 — strike column contrast | MEIC + Manual active tables; strikes low contrast on dark theme |
| Jun 25, 2026 | V2.5 implementation plan | `MEIC-with-Dash-main-V2.5/docs/IMPLEMENTATION.md` |

---

# Incident deep dive (Jun 25 afternoon)

**Investigation script:** `scripts/investigate_jun25_incidents.py`

```bash
python scripts/investigate_jun25_incidents.py              # offline replay (credit band, dashboard pick)
python scripts/investigate_jun25_incidents.py --broker     # TastyTrade order lookup + fill_sync dry-run
```

---

## A — 01-15 put never placed (two separate bugs)

### Timeline

| Time | Event |
|------|--------|
| 13:14:03 | Launcher fires tranche 01-15 |
| 13:14:05–06 | Put thread scans only; **no** `Attempt 1: placing spread` |
| 13:14:10–17 | Call fills 7390/7415 @ $1.40 (order 478858476), stopped out later |

### Bug A1 — Overlap shift rejected for **credit band**, not overlap

**First scan hit:** 7325/7300 @ $1.70 (API mids 2.55 / 0.82).

**Leg conflict:** long **7300** = open **manual ms-5** short **7300** (`leg_overlap_conflict`).

**Shift attempt:** 7325/7300 → **7335/7310** (+$5 PCS rule).

| Check | Result |
|-------|--------|
| Leg overlap on 7335/7310 | **Clear** (no flip with ms-5, 12-00, etc.) |
| Credit on shifted strikes | **Fails** MEIC band $0.90–$1.85 |

Replay (from 01-15 log mids + stream estimate for 7310 at 13:14):

| Spread | Short mid | Long mid | Raw credit | Rounded | In band? |
|--------|-----------|----------|------------|---------|----------|
| 7325/7300 | 2.55 | 0.82 | 1.73 | 1.70 | Yes |
| 7320/7295 | 1.98 | 0.68 | 1.30 | 1.25 | Yes |
| **7335/7310** | ~2.10 | ~1.33 | **0.77** | **0.75** | **No** (< $0.90) |

Moving the long leg from **7300 → 7310** added ~$0.50 of long premium (7310 was much richer than 7300 at that moment), which collapsed spread credit below `CREDIT_MIN`. Warning text *“still conflicts or out of credit band”* is ambiguous; here it was **credit**.

### Bug A2 — Valid second candidate ignored (`candidates[0]`)

Second scan line: **7320/7295 @ $1.30** — **no leg overlap** (verified at incident time).

Scan collects both candidates but `open_spread_tt` always uses **`candidates[0]`** (the overlapping 7325/7300 row). Raises `TerminateRequest` → **no order, no JSON**.

`TerminateRequest` is **not** written to `01-15_put.log` (stdout only).

### Planned fixes (add to Change 1 scope)

7. **Scan pick:** Return first **non-`overlap_warning`** candidate, or `[clean]` only from early-return path (`spread_scan.py` line ~393–394 should not return full list when `max_results=1`).
8. **Shift logging:** Distinguish “credit below min” vs “overlap remains” in overlap-shift warning.

---

## B — 02-00 put breached; dashboard showed “open”

### What actually happened (broker-verified)

| Attempt | Order | Credit | Broker status | JSON file |
|---------|-------|--------|---------------|-----------|
| 1 | 478885114 | $1.60 | **cancelled**, 0 fill | `…P_885114.json` ghost |
| 2 | 478885165 | $1.40 | **cancelled**, 0 fill | `…P_885165.json` ghost |
| 3 | 478885227 | $1.35 | **filled** 1/1 | `…P_885227.json` real |

Real trade timeline (`885227.json`):

- Filled 13:59:26 — 7340/7315 @ $1.35  
- Stop 478885235 @ $3.20 placed  
- Stop **filled 14:03:26** (`exchange_stop`)  
- Long closed 14:04:03  

**Risk path worked.** Display was wrong.

### Why dashboard hid the breach

Three files match `02-00` + `P`. `build_summary()` uses **`matching[0]`** (lowest filename / order id) → **885114** ghost (`pending_fill`, credit $1.60, no stop).

`_slot_state_from_trade()` maps `pending_fill` → **`open`**, so grid showed a working put while the real row was **closed / breached** in `885227.json`.

Integration report (`trades/integration_report.json`) correctly logged all three `open_order` events including final **FILLED** on 885227.

### Manual fix

Delete ghost files `…P_885114.json` and `…P_885165.json` from `trades/active/MEIC_IC/`.

---

## C — Manual ms-8 (3-lot put): order **865557** filled; bot never picked it up

This matches the dashboard screenshot: **ms-8** shows **7335/7310**, limit **$0.90**, **0/3**, state **Working** — while TastyTrade had **3/3 filled** on a different order number than the filename suggests.

### Order timeline (two order numbers, one JSON file)

Manual **Modify Price** creates a **cancel + replace** chain. Only the **JSON** is updated; the **filename** stays on the first order id.

| Step | Time (approx) | Order | Limit | What happened |
|------|---------------|-------|-------|----------------|
| 1. Place | ~13:24:15 | **478865168** | $1.00 | Dashboard **Place** → handshake file `…P_865168.json` created |
| 2. Modify | ~13:24:49 | **478865557** | $0.90 | **Modify Price** cancels 865168, places new spread order |
| 3. Fill | shortly after | **478865557** | — | Broker **filled 3/3** @ **$0.95** credit (short $1.42, long $0.47) |

**Broker lookup (Jun 25 investigation):**

| Order | Status | Fills |
|-------|--------|-------|
| 478865168 | **cancelled** | 0/3 |
| **478865557** | **filled** | **3/3** |

So **yes — the fill happened on 865557**, not on the order id in the filename (865168).

### Did the bot have the wrong order number?

**Two different consumers of the order id:**

| Component | Which order id? | Kept up with modify? |
|-----------|-----------------|----------------------|
| **JSON on disk** (`open_order_id`) | **478865557** | **Yes** — `modify_spread()` writes new id to disk |
| **Dashboard** (reads JSON files) | **478865557** | **Yes** — shows limit $0.90, strikes 7335/7310 |
| **Stop monitor thread** (`StopMonitor.self.state`) | Loaded **once** at thread start | **Often no** — see below |

`modify_spread()` correctly updates disk:

```266:283:manual_spread/entry.py
    state['open_order_id'] = str(result.order_id)
    ...
    state_mod.save_state(path, state)
    ...
    sync_open_order(state, broker, force=True, min_interval_sec=0)
    state_mod.save_state(path, state)
```

The **dashboard was not looking at the old order id** — it showed **0/3** because **fill data never landed in the JSON**, not because it tracked 865168.

### Root cause: stop monitor polls a **stale in-memory** order id

Investigate with: `python scripts/investigate_jun25_incidents.py --broker`

`StopMonitor` loads trade JSON **once** when the thread starts:

```72:72:blocks/stop/monitor.py
        self.state = state_mod.load_state(json_path)
```

Every fill poll uses that cached value:

```223:232:blocks/stop/monitor.py
    def _sync_entry_fills(self) -> None:
        if not self.state.get('open_order_id'):
            return
        ...
        changed, _ = sync_open_order(self.state, self.broker)
```

**Typical failure sequence for ms-8:**

1. **Place** writes `…865168.json`; runner starts a monitor thread within a few seconds → memory has **`open_order_id = 478865168`**.
2. Monitor may sync 865168 once (`in flight` / working).
3. **Modify Price** (~34s later) cancels 865168, places **865557**, saves JSON on disk with **`open_order_id = 478865557`** and one `in flight` snapshot (`last_sync` **13:24:49**).
4. Monitor thread **still polls 478865168** (cancelled, 0 fills) — it never queries **478865557** where the fill occurred.
5. Cancelled polls often produce **`changed=False`** → **no `save_state`** → heartbeat stuck at 13:24:49 (`module_start_count: 1`).
6. JSON on disk keeps **865557** but **`filled_quantity: 0`** forever → dashboard **0/3 Working**; **`active_stop: null`** → no stop.

```mermaid
sequenceDiagram
    participant UI as Dashboard
    participant MS as manual_spread/entry
    participant Disk as trades/active JSON
    participant SM as StopMonitor thread
    participant TT as TastyTrade

    UI->>MS: Place @ $1.00
    MS->>TT: order 865168
    MS->>Disk: open_order_id=865168
    SM->>Disk: load once → memory 865168
    UI->>MS: Modify @ $0.90
    MS->>TT: cancel 865168, order 865557
    MS->>Disk: open_order_id=865557 (in flight)
    Note over SM: Still polls 865168 in memory
    TT-->>TT: 865557 filled 3/3
    SM->>TT: get_order_status(865168)
    TT-->>SM: cancelled 0/3
    Note over Disk,UI: JSON never gets fills; UI shows 0/3 Working
```

### Why `fill_sync` dry-run works today but the bot didn’t

Running `investigate_jun25_incidents.py --broker` loads JSON from disk (**865557**) and syncs → **open**, 3/3, leg prices. That proves:

- The **correct** order id is on disk after modify.
- Broker fill data is available.
- The gap was **stop monitor not polling that id** (stale memory), not a bad broker response.

### Operator impact

| Layer | ms-8 reality |
|-------|----------------|
| **TastyTrade** | 3-lot 7335/7310 PCS filled on **865557** |
| **Dashboard** | **0/3 Working** (JSON never got fills) |
| **Stop monitor** | No stop — never reached `status: open` |

### Planned fixes (Case C — superseded by Change 4)

With the entry monitor architecture, **working-order fill sync is entry-monitor scope**, not stop monitor:

- Entry monitor polls broker + updates the one JSON through place, modify, and chase.
- Stop monitor **does not start** until `filled_quantity >= target_quantity` (see Change 4).
- Stable filename without order-id suffix removes ms-8 filename confusion.

Interim hotfix (before entry monitor ships): reload `open_order_id` from disk in stop monitor — **not recommended** as the long-term fix; ms-8 class of bug goes away when stop threads no longer run on `pending_fill`.

Reconciliation tool: `python scripts/investigate_jun25_incidents.py --broker`

### Immediate recovery

```bash
python scripts/investigate_jun25_incidents.py --broker
```

If 865557 is still filled and position open, promote JSON to `open` (apply fill_sync + save) so stop_monitor can place the exchange stop — or protect manually on TT until reconciled.

---

## Summary table

| Case | Symptom | Root cause | Broker truth |
|------|---------|------------|--------------|
| 01-15 P | No put | Shift failed **credit min**; valid 7320/7295 ignored (`candidates[0]`) | No 01-15 put order |
| 02-00 P | Breach not on dashboard | **Two ghost JSONs**; dashboard picks 885114 | 885227 filled & stopped |
| ms-8 3× P | 0/3 Working, no stop | **Monitor polled cancelled 865168**; fill on **865557** never synced | **865557 filled 3/3** |

---

# Change 3 — Dashboard strike column contrast (MEIC + Manual)

**Date observed:** Jun 25, 2026  
**Status:** Documented — CSS fix deferred

## Problem

On the dark theme (`#0f1117` / `#1a1d27` cards), **strike prices are hard to read** in both grids:

| Table | Column | Symptom |
|-------|--------|---------|
| **MEIC tranche grid** | Short / Long strike columns | Values appear **very low contrast** (dark grey on dark row) |
| **Manual spreads — Active** | **STRIKES** (combined `7335/7310`) | Same — strikes barely visible; LOT, LIMIT, P&L remain readable |

Example from live session: ms-8 row shows **7335/7310** nearly illegible while **$0.90**, **0/3**, and **Working** are clear.

## Cause

High-contrast CSS was added for **manual scan candidates** (`#ms-candidates tr.ms-cand-row .font-monospace { color: #e2e8f0 }`) but **not** for:

- MEIC grid cells: `renderGrid()` uses `class="font-monospace"` on strike columns **without** a light text class (`index.html` ~732–733).
- Manual active table: `renderManual()` uses `class="font-monospace"` on STRIKES **without** explicit color (`index.html` ~640).

Bootstrap `.font-monospace` inherits body color, but row/state backgrounds and missing row-level `color` on `#ms-active-tbody td` let strike cells render **muted/dim** compared to adjacent columns.

## Planned fix

1. Add shared rule, e.g. `.grid-strike, .tranche-row .font-monospace, #ms-active-tbody .font-monospace { color: #e2e8f0; font-weight: 500; }`
2. Optionally use `text-info` / `#a5b4fc` for strikes to match entry credit styling.
3. Verify in both **MEIC** and **Manual** tabs at default zoom on GCP dashboard.

## Acceptance

- Strike values readable at a glance on `#0f1117` without squinting.
- No regression on scan candidate table (already fixed).

---

# Change 4 — Entry monitor (separate from stop monitor)

**Status:** Documented — reconciled; Q1/Q2/Q6/Q7 locked; Q3–Q5/Q8 use listed defaults unless overridden  
**Motivation:** Ghost JSON on MEIC retry (Change 1), ms-8 fill sync, future indicator/complex entry

## Architecture overview

Two long-lived services, mirroring each other:

| Service | Owns | Does **not** own |
|---------|------|------------------|
| **Entry monitor** (`blocks/entry/runner.py`) | When/how to **open**; scan → place → poll/chase; one JSON per trade; `order_history[]` | Kill, manual close, exchange stop, breach |
| **Stop monitor** (`blocks/stop/runner.py`) | Stops, breach, kill, close — **only after full entry** | Entry chase, place, modify |

**Cancel semantics:** Entry monitor may cancel a **working open order** only as part of `on_unfilled` chase (MEIC) or when giving up after `max_attempts`. Operator **Kill** and spread **close** stay in stop monitor (Change 2).

### Entry monitor runner (mirror stop monitor)

Same pattern as `blocks/stop/runner.py`:

```
External loop (single-threaded supervisor)
  → scan session CSV each tick
  → for each row: entry window open? not paused? state == pending?
       → spawn worker (thread) for that row’s scan/place/chase/handoff
       → mark row state entering (or entered path) — **do not fire again**
  → move on to next row (11:00 P and 11:00 C can run **in parallel**)
```

**Fire once per row per day:** While `entry_window_start` ≤ now ≤ `entry_window_end`, trigger **only if** `state == pending`. After spawn, set `state: entering` immediately so repeated polls inside the window (e.g. 10:59–11:05) do **not** start a second entry. Terminal states: `entered`, `skipped`, `failed` — never auto re-fire unless operator resets to `pending`.

**Per-side independence:** `01-15_P` and `01-15_C` are separate rows — separate workers, separate trade JSONs. Pause one side without affecting the other.

### Entry flow (three layers)

```
strategies.yaml          defaults (deploy-time)
       ↓ morning bootstrap (MEIC) or UI “Take Trade” (Manual)
session plan CSV         today’s executable rows — dashboard + entry monitor
       ↓ full fill
trade JSON               one position — stop monitor
```

1. **Resolve row** — entry monitor reads **session plan** row (MEIC: time reached + not paused; Manual: operator armed row).
2. **Scan & place** — row’s structure (strikes or scan params), qty, chase sequence.
3. **Poll / chase** — one trade JSON; `order_history[]`; chase only at zero fill.
4. **Hand off** — snapshot row’s **stop** settings onto trade JSON → `status: open` → stop runner.

### Partial fills (locked)

| `filled_quantity` | Entry monitor | Stop monitor |
|-------------------|---------------|--------------|
| `0` | Poll; **chase allowed** after `fill_wait_sec` if policy ≠ `none` | No thread |
| `1 … target−1` | **Poll only — no chase**; keep working order until rest fills or operator cancels | No thread |
| `≥ target` | Done → `status: open` | Start → place exchange stop |

Chasing with a partial on the book adds complexity without benefit. If price moves against a partial, we still need the **remaining** quantity filled before a stop applies.

## JSON file model

**Filename:** `trades/active/{strategy}/{slot}_{P|C}_{entry_ts}.json`

- MEIC: `01-15_P_20260625T131403.json`
- Manual: `ms8_P_20260625T132415.json` (slot = `ms-N`)

Same file through all chase attempts. Order id lives in JSON fields, not the filename (fixes ms-8 confusion).

**`order_history[]` row shape:**

```json
{
  "order_id": "478865557",
  "limit_credit": 0.90,
  "short_strike": 7335,
  "long_strike": 7310,
  "status": "filled",
  "filled_quantity": 3,
  "ts": "2026-06-25T13:24:49Z",
  "reason": "placed | cancelled_for_chase | replaced_manual | filled | rejected",
  "on_unfilled_step": "chase_same_trade"
}
```

## Strategy model — defaults vs session plan

A **strategy** combines seven dimensions. **`strategies.yaml`** holds **defaults** only. Each trading day, defaults copy into a **session plan** that **dashboard and entry monitor both read**. Operator edits update the **session plan**, not YAML.

| # | Dimension | Examples |
|---|-----------|----------|
| 1 | **Entry conditions** | Time slot, indicator, Take Trade button, stop-out recovery |
| 2 | **Trade type** | PCS (`P`), CCS (`C`) |
| 3 | **Trade structure** | Width, credit band, delta, explicit strikes (manual) |
| 4 | **Underlying** | SPX (v1) |
| 5 | **Stop conditions** | 2×–5× multiplier or **stop %** (100% cr ≈ 2×) |
| 6 | **Average conditions** | Scale-in — **future** |
| 7 | **Stop triggers new trade** | Martingale recovery row — **future** |

| Layer | File | Who writes | Who reads |
|-------|------|------------|-----------|
| **Defaults** | `config/strategies.yaml` | Dev / deploy | Morning bootstrap |
| **Session plan** | `trades/session/{strategy}_{date}.csv` | Bootstrap + **dashboard** | **Dashboard grid**, **entry monitor** |
| **Trade JSON** | `trades/active/{strategy}/…` | Entry monitor | Dashboard overlay, **stop monitor** |

**MEIC:** Bootstrap at session start → one row per lot × side. MEIC grid **is** these rows.

**Manual:** Same row shape; row created when operator builds trade in UI (no morning copy). Entry = **Take Trade**.

**Live edits:** Dashboard updates row; entry monitor reloads each poll (`pending` → apply; `entering` → pause only; `entered` → trade JSON owns stop).

**Martingale (future):** Append new session row (linked via `parent_trade_path`), not edit YAML.

## Strategy defaults (`strategies.yaml`)

```yaml
strategies:
  - name: MEIC_IC
    enabled: true
    instrument: SPX
    defaults:
      stop: { mode: multiplier, multiplier: 2 }
      structure:
        type: credit_spread
        width: 25
        credit_min: 0.90
        credit_max: 1.85
      entry:
        fill_wait_sec: 5
        max_attempts: 10
        chase_sequence:
          - { mode: chase_same_trade, max_attempts: 3 }
          - { mode: build_new_strikes, max_attempts: 10 }

  - name: MANUAL_SPREAD
    enabled: true
    instrument: SPX
    defaults:
      stop: { mode: multiplier, multiplier: 2 }
      entry:
        on_unfilled: none
        fill_wait_sec: 5
        chase_sequence: []
```

Tranche clock times stay in launcher config; bootstrap expands into MEIC session rows.

## Session plan format (CSV)

**Why CSV:** One file per strategy per day — easy to open in Excel, edit pause/qty/stop/chase, and see **all steps (rows) in one place**. YAML stays deploy-time defaults only; **CSV is today’s executable plan**.

**Path (locked):** one CSV per strategy per day:

- `trades/session/MEIC_IC_2026-06-25.csv` — all MEIC rows for that day  
- `trades/session/MANUAL_SPREAD_2026-06-25.csv` — manual rows appended when operator uses Take Trade  

No combined cross-strategy file.

**MEIC example** (header + one row):

```csv
slot_key,lot,side,entry_window_start,entry_window_end,entry_condition,paused,skip,quantity,stop_mode,stop_multiplier,stop_percent,width,credit_min,credit_max,chase1_mode,chase1_max,chase2_mode,chase2_max,fill_wait_sec,max_attempts,state,trade_path
01-15_P,01-15,P,13:14:00,13:20:00,time_slot,false,false,1,multiplier,2,,25,0.90,1.85,chase_same_trade,3,build_new_strikes,10,5,10,pending,
01-15_C,01-15,C,13:14:00,13:20:00,time_slot,false,false,1,multiplier,2,,25,0.90,1.85,chase_same_trade,3,build_new_strikes,10,5,10,pending,
02-00_C,02-00,C,13:59:00,14:05:00,time_slot,false,false,1,multiplier,3,,25,0.90,1.85,chase_same_trade,3,build_new_strikes,10,5,10,pending,
```

**Manual row** (UI appends when operator arms Take Trade — strike columns filled, width/credit band empty):

```csv
slot_key,side,entry_condition,quantity,stop_mode,stop_multiplier,short_strike,long_strike,limit_credit,chase1_mode,chase1_max,on_unfilled,state,trade_path
ms8_P,P,manual,3,multiplier,2,7335,7310,0.90,,,none,pending,
```

**Column notes:**

| Columns | Purpose |
|---------|---------|
| `entry_window_start`, `entry_window_end` | Same as today’s `TrancheSlot` window; entry monitor fires while clock in window |
| `paused`, `skip` | Dashboard pause / skip volatile slots (`true`/`false`) |
| `stop_mode`, `stop_multiplier`, `stop_percent` | v1: `multiplier,2`. Future: `percent,100` (≈2× cr) |
| `chase1_*`, `chase2_*` | Flattened chase sequence (expand columns if needed) |
| `state` | `pending` → `entering` → `entered` / `skipped` / `failed` |
| `trade_path` | Set when trade JSON exists; dashboard overlays live fills |

**Read/write:** **Dashboard only** for operator edits (not Excel). Dashboard and entry monitor parse CSV each refresh; atomic write (temp → rename).

**Bootstrap (locked):** **Launcher** creates `MEIC_IC_{date}.csv` at morning cleanup (~8:29) **or on later startup if missing**. If file **already exists** (e.g. bot crash restart), **skip** bootstrap — preserve operator edits and row states.

**Trade JSON stays JSON** — nested broker/handshake fields do not belong in CSV; session CSV is the **plan**, trade JSON is the **executed position**.

### Unfilled / chase policy

After each `fill_wait_sec` wait, behaviour depends on fill state and strategy:

| Mode | When `filled_quantity == 0` | Typical use |
|------|----------------------------|-------------|
| **`none`** | Keep working order; poll only | Manual |
| **`chase_same_trade`** | Cancel → re-place **same strikes**, lower limit (PCS credit ↓; CCS debit ↑) | MEIC credit chase |
| **`build_new_strikes`** | Cancel → full re-scan (cr band + overlap) → new strikes | MEIC overlap / empty band |
| **`chase_sequence`** | Run phases in order, each with its own `max_attempts` | **MEIC default** |

**Suffix shorthand:** `chase_same_trade_10x` parses as `mode=chase_same_trade, max_attempts=10`. The number is configurable (`_3x`, `_10x`, etc.), not a fixed enum.

**MEIC default v1:** `chase_same_trade ×3`, then `build_new_strikes ×10`.

**Chase price step (locked):** $0.05 per `chase_same_trade` attempt on the **spread** limit. If we ever chase at individual-leg level, apply existing **$3 minimum premium** rules separately — v1 entry chase is spread-level only.

Auto-cancel applies **only when `filled_quantity == 0`** and `on_unfilled` is not `none`.

**Max retries exhausted:** Row `state: failed`; trade JSON `entry_failed` if applicable; log + alert.

## Dashboard ↔ session plan (MEIC)

MEIC grid rows **are** session plan rows (plus live trade JSON overlay when `trade_path` set).

| Operator action | Session row change |
|-----------------|-------------------|
| Pause Selected / side | `paused: true` on `{lot}_{side}` |
| Pause All MEIC | all MEIC rows `paused: true` |
| Skip / volatile day | `skip: true` or `paused: true` on future rows |
| Change quantity | `quantity` |
| Widen stop 2× → 3× | `stop_multiplier` column (before entry only) |
| Change chase | `chase1_*`, `chase2_*` columns (before entry only) |

Entry monitor polls session file; no separate pause file required once migrated.

**Stop change after fill, before exchange stop placed (locked):** Operator widens stop on dashboard (e.g. 2× → 3×) and **submits** → dashboard updates session CSV row **and** trade JSON `stop` snapshot → **one-shot action** (API/subprocess) tells stop monitor to place/replace stop at new level. No continuous CSV watch loop for this.

## Manual dashboard (UI-driven rows)

Same row schema; operator builds via UI:

| Field | v1 | Planned UI |
|-------|-----|------------|
| Strikes + limit | Scan + select | unchanged |
| Quantity | Input | unchanged |
| Entry | **Take Trade** → row `state: entering` | unchanged |
| Stop | Default **2×** | Selectable **2×–5×** or **%** (100% cr ≈ 2×) |
| Chase | `none` (no auto chase) | Selectable `chase_sequence` |
| Modify / Cancel | Update row + trade JSON | entry monitor executes |

Flow: UI writes/updates **session row** → entry monitor places → dashboard polls **trade JSON**. No morning bootstrap for manual.

## Runtime command queue (optional, narrow)

**Primary MEIC driver is session plan**, not a command queue. Optional thin queue for **immediate** actions only:

| Command | Use |
|---------|-----|
| `modify` / `cancel` | Manual working-order changes (if not written straight to session row) |

Kill Selected / Kill All → stop-monitor sentinels (`*.close.json`, `killswitch.json`) — unchanged (Change 2).

## Entry → stop handoff (JSON contract)

When entry monitor completes a **full fill**, it must write everything stop monitor needs **before** setting `status: open`. Stop runner gate: only start a thread when this contract is satisfied.

**Required on handoff** (aligns with `blocks/stop/state.py` `create_new_state`):

| Field | Source | Stop monitor uses for |
|-------|--------|------------------------|
| `status` | `'open'` | Runner gate |
| `quantity` / `filled_quantity` | Target qty, fully filled | Stop size |
| `stop_profile` / strategy metadata | **Snapshot from session row** `stop` | 2× vs 3× stop pricing |
| `open_order_id` | Final broker order | Audit, fill_sync if needed |
| `open_order.fully_filled` | `true` | Confirm entry complete |
| `entry.strategy`, `lot`, `side`, `net_credit`, `two_x_net_credit` | From fill sync | P&L, dashboard |
| `short_leg` / `long_leg` | `symbol`, `strike`, **`fill_price`** | **Stop limit** (`two_x_short` derived from short fill) |
| `order_history[]` | Full entry audit trail | Dashboard, debug |

Entry monitor runs `fill_sync` (or equivalent) on the final order, computes `two_x_net_credit` / `two_x_short`, saves JSON, **then** stop runner’s next scan starts `StopMonitor`.

**While `pending_fill`:** JSON may be incomplete; stop monitor **must not** attach. Dashboard reads same file for Working state.

**Acceptance:** Integration test — session row with `stop_multiplier=3` → full fill → trade JSON has correct `two_x_*` → stop placed at 3×. **Implemented Jun 26, 2026:** `blocks/stop/stop_math.py`, `setup_initial_stop()` reads `stop_multiplier` from trade JSON; tests in `tests/test_stop_multiplier.py`. See [LIVE_SESSION_2026-06-26.md](LIVE_SESSION_2026-06-26.md).

## Triggers

| Trigger | Today | After |
|---------|-------|-------|
| MEIC tranche window | Orchestrator → `vertical_thin` (one subprocess, **both** P+C) | Entry monitor executes **each CSV row** (P and C independent; pause one side OK) |
| Manual Take Trade | Dashboard → `manual_spread/entry` | UI writes **session row** → entry monitor |
| Pause / skip / qty / stop / chase | `pause_tranches.json` | **Session row** edit (dashboard) |
| Stop-out recovery | — | Append **session row** (future martingale) |
| Indicators | — | Append or activate session row (future) |

## Code migration map

| Source (today) | Role | After Change 4 |
|----------------|------|----------------|
| `meic0dte/app/vertical_thin.py` | Subprocess retry loop (both sides per lot) | **Retire** — entry monitor executes **per CSV row** (side) |
| `meic0dte/open/open_spread_tt.py` | Scan, place, write JSON, wait for fill | **Entry monitor pipeline** (shared) |
| `meic0dte/open/spread_scan.py` | OTM grid, credit band, overlap | **Entry monitor** + fix `candidates[0]` |
| `manual_spread/entry.py` | Dashboard → broker directly | **Thin API** → session row + entry monitor |
| `blocks/stop/fill_sync.py` | Poll broker, update JSON | **Entry monitor** while opening; stop monitor minimal use after `open` |
| `blocks/stop/runner.py` | Spawn stop thread per active JSON | **Gate:** only `status: open` + full fill |
| `blocks/stop/monitor.py` | Stops, breach, kill, close + `_sync_entry_fills` | **Simplify** — drop entry fill sync and partial-stop resize; only runs on completed entries |
| `blocks/entry/handshake.py` | Initial JSON / filename | Update for stable naming |

**New modules:** `blocks/entry/runner.py`, `blocks/entry/monitor.py` (mirror stop block structure).

## Incident → change mapping

| Incident | Primary fix | Notes |
|----------|-------------|-------|
| 01-15 put (no order) | **#4** scan pick + pipeline | Overlap-shift log clarity → **#1** |
| 02-00 put (breach hidden) | **#1** one JSON + dashboard pick | Ghost files from retry |
| ms-8 (fill not synced) | **#4** entry monitor owns working-order sync | Stop monitor no longer runs on `pending_fill` |

Regression: `python scripts/investigate_jun25_incidents.py --broker`

## Implementation phases

| Phase | Delivers | Depends on |
|-------|----------|------------|
| **4a** | One JSON per trade; stable filename; `order_history[]`; fix `candidates[0]` | — |
| **4b** | Dashboard prefers filled/real over ghost `pending_fill` (**#1**) | 4a |
| **4c** | Session bootstrap + entry monitor; MEIC rows from session plan; retire `vertical_thin` | 4a |
| **4d** | Manual UI → session row; stop/chase selectors (v1 default 2× / none); JSON poll | 4c |
| **4e** | Stop runner gate (`open` + full fill only); remove `_sync_entry_fills` / partial-stop from stop monitor | 4c |
| **4f** | Full `chase_sequence` combo + tests | 4c |

**Suggested order:** 4a+4b → 4e+4c → 4d → 4f. Change 2 parallel.

## Acceptance

- MEIC: session plan bootstrap; dashboard grid ↔ rows; pause/skip/qty/stop/chase on row; entry monitor executes.
- Manual: UI-built session row; v1 stop 2× default; chase none; future stop 2×–5× / % and selectable chase.
- Partial entry: poll until full or operator cancel — never auto-cancel for chase.
- Tests: zero-fill chase, partial no-chase, credit band, overlap pick, dashboard ghost pick, **handoff JSON → stop placed**.

## Deferred (not blocking v1)

| Area | Feature |
|------|---------|
| Entry | Indicator conditions; delta-targeted scan |
| Underlying | ES, SPY per strategy |
| Average (6) | Scale-in / avg-down rules |
| Martingale (7) | Recovery rows after stop-out |
| Manual UI | Stop selector 2×–5× or unified **%**; selectable **chase_sequence** |
| Other | Modify after `open`; partial-fill chase or partial-stop |

---

## Implementation details (locked)

| # | Topic | Decision |
|---|--------|----------|
| 1 | **Change 2 scope** | **Kill Selected / Kill All** → one spread close. **Software breach unchanged.** Exchange stop unchanged. |
| 2 | **Chase limit step** | **$0.05** per spread on `chase_same_trade`. Leg-level chase (if ever) uses $3 min-premium rules separately. |
| 3 | **Retries exhausted** | Terminal status `entry_failed` / `max_retries_reached` on trade JSON + log/alert. |
| 4 | **Session plan** | **One CSV per strategy per day:** `trades/session/{strategy}_{date}.csv`. MEIC + Manual separate files. |
| 5 | **Stop monitor simplification** | Runner gate: full fill only; drop `_sync_entry_fills` and partial-stop resize. |
| 6 | **Entry runner (Q1)** | Supervisor scans CSV; **spawn worker per row in parallel**; **fire once** per row (`pending` → `entering` inside window). |
| 7 | **CSV bootstrap (Q2)** | Launcher at ~8:29 or on startup **if file missing**; if exists, **skip** (crash recovery). |
| 8 | **Post-fill stop edit (Q6)** | **Honor** via dashboard submit → update CSV + trade JSON → one-shot stop (re)place. |
| 9 | **Operator edits (Q7)** | **Dashboard only** — no Excel workflow in v1. |

---

## Open questions

**All locked** (Jun 25, 2026). See V2.5 implementation plan §2.

| # | Decision |
|---|----------|
| Q3 | Retire orchestrator + `vertical_thin` after 4c |
| Q4 | One wide CSV header (MEIC + optional manual columns) |
| Q5 | Drop `pause_tranches.json` when CSV grid ships |
| Q8 | Keep `failed` / `skipped` rows; reset to `pending` to re-run |

