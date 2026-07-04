# MEIC Integration & Test Guide

Known behavioral gaps vs legacy Schwab: [SYSTEM_GAPS.md](SYSTEM_GAPS.md).

This document covers **how to validate** the full MEIC pipeline: tranche entry, MQTT streamer, `stop_monitor`, TastyTrade order placement, and stop/breach logic.

For project setup (uv, `.env`, OAuth), see [README.md](README.md).

---

## Prerequisites


| Requirement                        | Check                                                 |
| ---------------------------------- | ----------------------------------------------------- |
| Python 3.11+ with `uv`             | `uv run python --version`                             |
| `.env` with TastyTrade credentials | `uv run python tests/adhoc_integration.py check-env`  |
| Mosquitto running locally          | `uv run python tests/adhoc_integration.py check-mqtt` |
| Broker auth                        | `uv run python tests/adhoc_integration.py check-auth` |
| All of the above                   | `uv run python tests/adhoc_integration.py check-all`  |


Dashboard (started by `run.py`): **[http://localhost:5002](http://localhost:5002)**

---

## Market data (single path)

All **quotes** (SPX + options) come from one pipeline:

```
TastyTrade DXLink  →  publish_tastytrade.py  →  Mosquitto MQTT  →  MqttPriceCache
                                                                    ├─ tranche (strike scan)
                                                                    └─ stop_monitor (breach)
```

- The **broker** places/cancels orders only — it does **not** open its own DXLink or REST price feeds.
- Tranche registers candidate symbols in `optsymbols.json`; the streamer subscribes and publishes mids to MQTT.
- `check-prices` and integration tests require the **streamer running** (or start via `run.py` / `--integration-session`).

---

## Test layers

```
Layer 1 — Offline unit tests     (no broker, no MQTT)
Layer 2 — Connectivity checks    (broker + MQTT only)
Layer 3 — Ad-hoc commands        (single action: place trade, seed JSON, etc.)
Layer 4 — Integration sessions   (full production path, timed MQTT report)
```

Run Layer 1 first on every change:

```powershell
uv run python tests/run_tests.py
```

---

## Scenario 1 — Off-hours tranche for next market open

**Goal:** After market close (e.g. Friday Jun 19), force one tranche targeting the **next trading day** expiry (e.g. Jun 22). Confirm PCS + CCS orders hit TastyTrade, streamer registers strikes, and MQTT quotes flow for ~5 minutes.

**Example context:** Market closed Jun 19 → target expiry `2026-06-22`.

### Command

```powershell
uv run python run.py --integration-session --expiry 2026-06-22 --duration 300
```

Equivalent adhoc wrapper:

```powershell
uv run python tests/adhoc_integration.py integration-session --expiry 2026-06-22 --duration 300
```

### What starts


| Process                                     | Role                           |
| ------------------------------------------- | ------------------------------ |
| `dashboard/server.py`                       | Web UI                         |
| `streaming/publish_tastytrade.py`           | TastyTrade DXLink → MQTT       |
| `stop_monitor/runner.py`                    | Watches `trades/active/*.json` |
| `meic0dte/app_main.py` → `vertical_thin.py` | One forced PCS + CCS tranche   |


### Environment set by integration session


| Variable           | Value             | Effect                                                                                                    |
| ------------------ | ----------------- | --------------------------------------------------------------------------------------------------------- |
| `MEIC_INTEGRATION` | `1`               | Register streamer symbols on **order place**; leave `WORKING` orders on book; skip 3 PM streamer shutdown |
| `MEIC_FORCE_TRADE` | `1`               | Skip trading-day / schedule gates                                                                         |
| `MEIC_EXPIRY`      | e.g. `2026-06-22` | Override expiry for strike selection                                                                      |


### Expected results (off-hours)


| Check                            | Pass criteria                                                         | Off-hours note                                  |
| -------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------- |
| TastyTrade orders                | PCS + CCS at target credit                                            | Likely `WORKING`, not filled                    |
| `trades/integration_report.json` | `open_order` events with `order_id`, side, strikes                    | Written for every place attempt                 |
| MQTT report                      | `SPX` + spread leg topics with message counts > 0                     | Symbols registered on place in integration mode |
| `trades/active/*.json`           | Handshake JSON with `open_order_id` (status `pending_fill` off-hours) | Written on **place**, not on fill               |
| `stop_monitor`                   | Running; **no stop** until paired spread units fill                   | Entry fill sync throttled to **60s** (see below) |


### Entry ↔ stop handshake (production behavior)

Placing an order does **not** place a stop. The entry thread and `stop_monitor` coordinate through `trades/active/*.json` using the **open order number** as the key:

```
vertical_thin places spread
        │
        ├─► trades/active/*.json     (status: pending_fill, open_order_id, target quantity)
        ├─► integration_report.json  (order_id, filled_quantity, strikes)
        ├─► optsymbols.json          (streamer symbols on place)
        │
        ├─► Entry thread waits up to FILL_WAIT_MAX (60s), syncing fills into JSON
        └─► Entry thread exits — stop_monitor owns the rest

stop_monitor picks up JSON
        │
        ├─► Syncs open_order_id from TastyTrade every 60s (stop_monitor/fill_sync.py)
        ├─► filled_quantity == 0  →  no stop placed
        ├─► filled_quantity == 2/5  →  stop on 2 paired spread units only
        ├─► New fills arrive  →  cancel stop, replace at higher qty
        └─► fully filled  →  Phase 1 / 2 / 3 monitoring as today
```


| Step                | Who          | What                                                                          |
| ------------------- | ------------ | ----------------------------------------------------------------------------- |
| Order place         | Entry thread | Write `pending_fill` JSON with `open_order_id` — **no stop**                  |
| Initial wait        | Entry thread | Poll broker up to **60s** (`FILL_WAIT_MAX`), update `filled_quantity` in JSON |
| Partial (e.g. 2/5) | stop_monitor | Paired spread units: stop for **2** (short+long filled together) |
| More fills          | stop_monitor | Cancel + replace stop at new qty (every 60s sync until `fully_filled`)        |
| Full fill           | stop_monitor | Normal stop + breach monitoring                                               |


**Off-hours:** JSON exists with `filled_quantity: 0`; streamer still registers strikes; **no stop** until market fills arrive.

### What is `MEIC_INTEGRATION=1`?

A **test-only** environment flag (set by `--integration-session` / `stop-session`). It is **not** used in normal production trading.


| Effect                                                                | Why it matters for tests                 |
| --------------------------------------------------------------------- | ---------------------------------------- |
| Registers streamer symbols when order is **placed** (not only filled) | MQTT quotes during off-hours integration |
| Leaves `WORKING` orders on the book (no cancel/retry)                 | Validate order IDs after Friday close    |
| Skips 3 PM streamer auto-shutdown                                     | 5-minute MQTT collection window          |
| Single open attempt per side                                          | Predictable integration run              |


Production `run.py` without this flag still uses the same **fill-aware handshake**; it cancels and retries if zero fills after 60s.

### Mental model (updated)

```
vertical_thin places order
        │
        ├─► trades/active/*.json  (handshake: open_order_id, pending_fill)
        ├─► integration_report.json
        └─► optsymbols.json / streamer
                    │
                    └─► stop_monitor: sync fills → stop qty = filled_quantity only
```

### Artifacts

- `trades/integration_report.json` — append-only order events
- Console — MQTT totals and per-symbol counts
- TastyTrade — working PCS/CCS order numbers

### Lighter variant (tranche only, no stop_monitor / MQTT report)

```powershell
uv run python run.py --integration-tranche --expiry 2026-06-22
```

---

## Scenario 2 — Stop flow on an existing position

**Goal:** Simulate “spread just filled” for a position you already hold, let `stop_monitor` place the exchange stop, then (optionally) test Phase 1 breach as a **second connected step**.

**Example position:** Jun 22 CCS 7635/7660, 5 contracts, open order #476911300. Default lot: `jun22-ccs-7635`.

### Step 1 — Seed + place exchange stop

Writes JSON, starts streamer + `stop_monitor`, places **STOP_LIMIT** on short 7635C.

```powershell
uv run python tests/adhoc_integration.py stop-session --seed --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id 476911300 --short-fill 1.45 --long-fill 0.85 --credit 0.6 --seconds 300
```

**Pass criteria (Step 1):**

| Check | Pass |
| ----- | ---- |
| `trades/active/*jun22ccs7635*.json` (lot `--lot jun22-ccs-7635`) | `active_stop.order_id` set, `type: STOP_LIMIT` |
| TastyTrade | Working `BUY_TO_CLOSE` stop on 7635C |
| Logs | `initial_2x_short` / stop placed |
| MQTT | Non-zero mids on short + long symbols |

### Step 2 — Breach simulation (no `--seed`)

**Do not re-seed.** Piggybacks on Step 1 JSON and broker stop. Flow:

1. Cancel exchange stop at broker → wait for confirmed cancel  
2. Place **LIMIT** `BUY_TO_CLOSE` on short at **live MQTT mid** (streamer), rounded to valid SPX tick  
3. Update JSON (`active_stop` → `LIMIT`, `stop_history`)

```powershell
uv run python tests/adhoc_integration.py stop-session --simulate-breach --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id 476911300 --short-fill 1.45 --long-fill 0.85 --credit 0.6 --seconds 300
```

If you pass `--seed` and `--simulate-breach` together, seed is **skipped** when the lot JSON already exists (use `--force-seed` to wipe).

**Why the limit is ~$0.15, not $4.45**

`--simulate-breach` injects **synthetic MQTT overrides** only to **trigger** breach (`spread_mid >= two_x_short + 0.20`). For your fills, overrides are roughly short **$4.45** / long **$0.85** → spread **$3.60** ≥ threshold **$3.10**.

The **limit order price** does **not** use those overrides. `replace_with_limit_close()` reads `MqttPriceCache.get_market_mid()` — the real streamer quote (~**$0.15** on 7635C today). That is intentional: breach detection can be simulated; the close price tracks the live market.

**Pass criteria (Step 2):**

| Check | Pass |
| ----- | ---- |
| TastyTrade | Exchange stop cancelled; new **LIMIT** on 7635C near live mid (~$0.15) |
| JSON | `active_stop.type` = `LIMIT`; `stop_history` has `breach_cancel` + `replaced_limit` |
| Logs | `spread_stop_breach`, limit price ≈ streamed mid (not $4.45) |

### SPX single-leg tick sizes

Cboe SPXW single-leg premiums use tiered ticks (`common/option_ticks.py`):

| Premium | Minimum increment |
| ------- | ----------------- |
| Below **$3.00** | **$0.05** |
| **$3.00** and above | **$0.10** |

All stop/limit prices on the short leg go through `round_spx_option_price()`. Legacy Schwab code used `>= 2.90 → round(..., 1)` in `meic0dte/open/fillaction.py`; the TastyTrade path now uses the $3.00 rule consistently.

Example: live mid **$0.15** → limit **$0.15**; mid **$4.45** (invalid) → **$4.50**.

### Handshake: order # + JSON

For **live tranches**, the entry thread writes JSON with `open_order_id` only; `stop_monitor` calls TastyTrade to populate fills.

For **existing positions** (this scenario), `seed-stop` writes fully-filled JSON. The `open_order_id` links back to entry order #476911300.

### What `stop-session` does

1. **`seed-stop`** (Step 1 only) — writes `trades/active/MEIC_IC_SPX_*_{lot}_*_{side}_*.json` (see Trade state files)
2. **Starts streamer** with `MEIC_INTEGRATION=1`
3. **`run-stop-monitor`** for `--seconds` (default 300)
4. **MQTT report** for SPX + 7635C + 7660C

### Manual two-step equivalent

```powershell
uv run python tests/adhoc_integration.py seed-stop --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id 476911300 --short-fill 1.45 --long-fill 0.85 --credit 0.6

uv run python tests/adhoc_integration.py run-stop-monitor --seconds 300 --lot jun22-ccs-7635
```

Step 2 breach (streamer must be running separately):

```powershell
uv run python tests/adhoc_integration.py run-stop-monitor --seconds 300 --simulate-breach --lot jun22-ccs-7635
```

> `run-stop-monitor` alone does **not** start the streamer. Use `stop-session` or run `streaming/publish_tastytrade.py` separately.

### Seeded JSON fields that matter

| Field | Used for |
| ----- | -------- |
| `short_leg.fill_price` | Exchange stop: `((fill - 0.10) × STOP_PRCNT)` → `round_spx_option_price` |
| `short_leg.two_x_short` | Breach threshold base: `round(fill × 2)` to valid tick |
| `long_leg.fill_price` | Phase 1 spread mid: `short_mid - long_mid` |
| `entry.net_credit` | Phase 2 upgrade (2× credit stop) |

### Skip auto-stop (place stop manually)

```powershell
uv run python tests/adhoc_integration.py seed-stop ... --skip-auto-stop
uv run python tests/adhoc_integration.py place-stop --side C --short-strike 7635 --quantity 5 --stop 3.0 --expiry 2026-06-22
```

---

## Trade state files (naming, collisions, MQTT)

### File naming (one trade = one JSON)

Live entry writes:

`MEIC_IC_SPX_{yymmdd}_{lot}_{HHMM}_{side}_{orderTail6}.json`

Example: `MEIC_IC_SPX_260620_1100_1135_C_113300.json` — Jun 20, **11-00** tranche, opened **11:35** Central, call side, open order `…113300`.

- **`lot`** — MEIC tranche label (`11-00`, `12-30`, … from `get_lot_time()`).
- **`HHMM`** — wall-clock when the JSON was created (so a later re-entry on the same strikes does not overwrite an earlier file).
- **`orderTail6`** — last 6 digits of `open_order_id` (unique per broker order).

`open_order_id` inside the JSON remains the handshake key for fill sync.

### Same strike / same side overlap (legacy guard)

Before opening, `get_open_spread_price_tt()` calls `common/strike_guard.py` (same rules as legacy `spreadprice.check_long_short`):

- On the **same side** (both calls or both puts), skip a candidate spread if the new **long** strike is already someone else’s **short**, or the new **short** is already someone else’s **long**.
- **Calls vs puts** are independent — a 7635C and 7635P can coexist.
- Scans **both** `meic0dte/trades/active/` and `manual_spread/trades/active/` (when present); same flip rule only — overlapping strike ranges without a leg flip are allowed.

### JSON on disk vs MQTT messaging

| Approach | Role today | Notes |
| -------- | ---------- | ----- |
| **`trades/active/*.json`** | Source of truth | Survives restarts; human-readable; one file per trade |
| **MQTT** | Market data only | Streamer → `MqttPriceCache`; not used for stop state |
| **AlertListener** | Fill events | Pushes stop-order fills into the monitor thread |

**Recommendation:** Keep JSON as the durable handshake. Use in-memory state in `MonitorRunner` during a run (already done for breach setup). Optional later: MQTT **events** (`MEIC/trade/{lot}/fill`) for dashboard speed — not a replacement for JSON until you need multi-machine consumers.

Windows Notepad++ locks: close JSON files while `stop_monitor` is running. Retries + in-memory reads reduce races; they do not fix an editor holding an exclusive lock.

---

## Stop monitor architecture

`stop_monitor/runner.py` (`MonitorRunner`) watches `trades/active/*.json` and runs **one `StopMonitor` thread per file**. The supervisor rescans for new JSON every ~3s; each monitor loop defaults to a **5s** poll (`--poll` on adhoc commands).

### Per-poll cycle (`StopMonitor._poll_once`)

1. **Entry fill sync** — `fill_sync.sync_open_order()` (broker `get_order_status` on `open_order_id`), throttled to **60s** (`FILL_SYNC_INTERVAL_SEC`) unless `force=True` on load.
2. **Stop placement / resize** — when `status: open` and `filled_quantity` grows, place or `_resize_stop()` for paired spread units only.
3. **Working stop sync** — `_sync_working_stop_order()` polls broker for `active_stop` fill/cancel (**every poll ~5s** today).
4. **Kill switch** — read from MQTT cache (`MqttPriceCache`).
5. **3:00 PM CT admin close** — `_finalize_close(reason='market_close_3pm')` when not in `MEIC_INTEGRATION` mode (JSON moved to `trades/closed/`; not the Phase 3 market-close path).
6. **Phase plugins** — Phase 1 → 2 → 3 by priority; first match wins for that cycle.

Thread **exits** when `status == 'closed'` after `state.move_to_closed()`; supervisor drops the handle once the file is under `trades/closed/`.

### MQTT / streamer-first (not broker quotes every poll)

All **decision prices** come from `MqttPriceCache` (Mosquitto ← `publish_tastytrade.py`). The broker is used for **orders and order status**, not for routine mid quotes.

| Use | Source |
| --- | --- |
| Phase 1 breach | **Spread mid** `short_mid − long_mid` vs `two_x_short + 0.20` — **not** short mid alone vs stop |
| Phase 1 breach limit / reprice | MQTT short mid → `round_spx_option_price()` |
| Phase 2 upgrade | Long leg MQTT mid ≤ $0.05 |
| Phase 3 proximity | SPX from MQTT vs short strike within **`STRK_IDX_DIFF` ($3)** |
| Long leg close (`_close_long_leg`) | MQTT long mid |
| Kill switch | MQTT topic |

Default **5s** poll reads the **MQTT cache** each cycle. Broker REST is **not** queried for option mids on every tick.

### Phase 3 vs 3:00 PM close

| Time (CT) | Behavior |
| --------- | -------- |
| **≥ 2:51 PM** (`STRK_CHK_MIN=51`) | **Phase 3** — if SPX within **$3** of short strike, cancel stop, **market** close short, `_close_long_leg()`, finalize |
| **≥ 3:00 PM** | **Admin close** — `_finalize_close('market_close_3pm')` without Phase 3 market logic (integration mode skips this) |

### Broker sync cadence (today vs desired)

| Concern | Today | Notes |
| ------- | ----- | ----- |
| Entry open-order fills | **60s** throttle | `FILL_SYNC_INTERVAL_SEC` in `fill_sync.py` |
| Working stop / breach limit status | **~5s** (every poll) | Faster than legacy ~30s; more API calls than ideal |
| Desired future | ~30s entry sync; ~30s–5min stop status | **Not implemented** — document only |

**Future recommendation (do not implement yet):** decouple a **fast MQTT breach path** (≤1s reaction on spread mid / kill switch) from **slow broker order sync** (~30s for entry fills and optional stop-status polls). Today one 5s loop handles both.

### Fill events

`AlertListener` (TastyTrade websocket) can push stop-order fills into the monitor thread’s queue when registered for `active_stop.order_id`. Polling remains the fallback every ~5s.

### Legacy vs new timing

| Concern | Legacy (`closetask.py` + `streamtask.py`) | New (`stop_monitor`) |
| ------- | ------------------------------------------ | -------------------- |
| Breach / strike check | **3s** loop (`await asyncio.sleep(3)`) | **5s** poll — **slower** breach reaction |
| Stop fill status check | ~**30s** (`count % 10` in 3s loop) | **~5s** poll — **faster** stop-fill detection |
| Streamer MQTT mids | Event-driven + re-publish every **5** × **1s** tick ≈ **5s** | Same `publish_tastytrade.py` |
| `streamtask` **1s** sleep | Re-subscribe / symbol-file poll only — **not** price logic | N/A (unified `MqttPriceCache`) |

Legacy `longclose.py` **replaced** the long limit on a timer until filled; the TastyTrade path places **one** `SELL_TO_CLOSE` limit at the streamed mid (no chase loop yet).

---

## Scenario 4 — Partial spread fill (paired legs) — **entry only**

> **Not the same as stop-out.** Scenario 4 tests **entry** when a working spread order partially fills. Scenario 5 tests **exit**: exchange stop on short fills → long leg close.

| | Scenario 4 (entry) | Scenario 5 (exit) |
| --- | --- | --- |
| Trigger | Open spread order partially / fully fills | `active_stop` on short leg fills (or sim) |
| Legs affected | Short **and** long open together per spread unit | Short closed by stop; long closed by monitor |
| `filled_quantity` | From open-order sync | Already set in JSON |
| Typical command | `partial-fill-session` | `test-long-close` / `stop-fill-session` |

**Goal:** When a spread order partially fills (e.g. **2 of 5**), those **2 units are full spreads** — short **and** long both fill 2 together. `stop_monitor` places a stop for that qty and **resizes** when more spread units fill.

**Example:** Jun 22 CCS 7635/7660, 5 contracts, open order #476911300. Lot: `jun22-ccs-partial`.

### How spread partial fills work

On a vertical spread order, the exchange matches **spread units**, not orphan legs:

- 2 of 5 filled ⇒ 2× short **and** 2× long opened together — **not** short leg filling before long  
- Broker (`tastytrade_broker.py`): `filled_quantity = min(short_filled, long_filled)`  
- `status: open` only when **both** leg fill prices are known (`fill_sync.apply_order_result_to_state`)

### Flow

1. Entry writes **pending** JSON (`open_order_id`, `pending_fill`).
2. `fill_sync.sync_open_order()` polls TastyTrade leg fills.
3. When paired units fill → `status: open` → stop for `filled_quantity`.
4. More units fill → `_resize_stop()` to new qty.

### Offline verification

```powershell
uv run python tests/run_tests.py
```

Covers `test_fill_sync.py` and `test_partial_fill_stop.py`.

### Simulated integration

`--simulate-partial-fill` wraps the broker so `get_order_status` returns **both** leg fill prices on Step 1 (paired spread units). Stop placement still hits TastyTrade.

| Step | Flag | Meaning |
| ---- | ---- | ------- |
| 1 | `--partial-step 1` + `--partial-qty 2` on a 5-lot | 2/5 spread units filled (short+long together) → stop for qty **2** |
| 2 | `--partial-step 2` (reuse Step 1 JSON) | Full 5/5 → cancel + resize stop to qty **5** |

Use `--partial-qty 2` on a 5-lot so Step 2 resizes 2 → 5.

#### Step 1 — 2 spread units filled; stop for qty 2

```powershell
uv run python tests/adhoc_integration.py partial-fill-session --simulate-partial-fill --partial-step 1 --partial-qty 2 --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id 476911300 --short-fill 1.45 --long-fill 0.85 --credit 0.6 --lot jun22-ccs-partial --seconds 120
```

**Pass criteria (Step 1):**

| Check | Pass |
| ----- | ---- |
| JSON | `status: open`, `filled_quantity: 2`, both leg fill prices set, `active_stop` for qty **2** |
| TastyTrade | `STOP_LIMIT` on 7635C for **qty 2** |

#### Step 2 — Full 5 units; resize stop

```powershell
uv run python tests/adhoc_integration.py partial-fill-session --simulate-partial-fill --partial-step 2 --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id 476911300 --short-fill 1.45 --long-fill 0.85 --credit 0.6 --lot jun22-ccs-partial --seconds 120
```

**Pass criteria (Step 2):** `filled_quantity: 5`, stop resized to qty 5.

### Live partial fill

Waits on real broker partial fills (paired spread units). Requires a **working** open order id at the broker.

```powershell
uv run python tests/adhoc_integration.py partial-fill-session --side C --expiry 2026-06-22 --short-strike 7635 --long-strike 7660 --quantity 5 --open-order-id <WORKING_ORDER_ID> --lot 12-00 --seconds 300
```

Use `--reuse-json` to skip rewriting pending JSON when the lot file already exists.
---

## Scenario 5 — Exchange stop fills → close long leg (**exit only**)

> **Not Scenario 4.** This is the **exit** path after the short-leg stop (or breach limit on short) fills — not partial **entry** fills on the opening spread order.

**Goal:** When the **short-leg `STOP_LIMIT`** fills at the broker (or breach `LIMIT` on short fills), `stop_monitor` must immediately place **`SELL_TO_CLOSE` LIMIT** on the long leg — same intent as legacy `closetask.py` / `longclose.py` after `short_close_flag=True`.

### Current code path (not removed)

```text
active_stop status = filled
  → handle_stop_order_update()
  → _close_long_leg()     # SELL_TO_CLOSE limit at MQTT mid
  → _finalize_close()     # JSON → trades/closed/
```

Legacy `longclose.py` **replaced** the long limit on a timer if not filled (chase/replace loop). The TastyTrade path places **one** limit at the MQTT streamed mid via `_close_long_leg()` (no chase loop yet).

### Production detection

- **AlertListener** (TastyTrade websocket) pushes fill events when registered for `active_stop.order_id`
- **Poll** every monitor cycle (~5s): `_sync_working_stop_order()` → `get_order_status` (falls back to `account.get_order` when the stop leaves the live book)

### Test on your Jun 22 CCS 7635/7660 position

Prerequisite: Scenario 2 Step 1 JSON with `active_stop` (`lot jun22-ccs-7635` or your partial-fill lot once fully stopped).

#### Option A — Long leg only (safest; short stop untouched)

Verifies the long close order path without simulating a stop-out:

```powershell
uv run python tests/adhoc_integration.py test-long-close --lot jun22-ccs-7635 --quantity 1
```

**Quantity behavior:**

| Flag | Qty used |
| ---- | -------- |
| (none) | JSON `filled_quantity` (e.g. 5 after `stop-session --seed`) |
| `--quantity 1` | **Overrides** JSON — places 1-lot long close only |

Use `--quantity 1` for a small live test. If you pass `--quantity 1` but still see qty 5 in the log, pull latest — older builds always preferred JSON over CLI.

**Concentration risk:** Closing the **full** long (5) while the short leg and its `STOP_LIMIT` are still open can be rejected by TastyTrade (`margin_check_failed_with_flags` / concentration risk). That is a broker rule, not a code bug. For a dry run, use `--quantity 1`; for the real exit chain use **Option B** (`stop-fill-session`), which cancels the short stop first.

Check TastyTrade for **SELL_TO_CLOSE** on **7660C**. JSON stays in `trades/active/`.

#### Option B — Full chain (simulated stop fill → long close)

Cancels your working short stop, then fakes “stop filled” so the monitor runs the full handler:

```powershell
uv run python tests/adhoc_integration.py stop-fill-session --lot jun22-ccs-7635 --quantity 5 --seconds 120
```

**Pass criteria:**

| Check | Pass |
| ----- | ---- |
| Logs | `Simulated stop fill`, `Long leg close placed`, `Spread closed` |
| TastyTrade | Prior short stop cancelled; **SELL_TO_CLOSE LIMIT** on 7660C near MQTT mid |
| JSON | File moved to `trades/closed/` with `close.reason: stop_filled` |

Use `--keep-live-stop` only if you understand you may have **both** a live short stop and a long close working.

### Offline

```powershell
uv run python tests/run_tests.py
```

Covers `test_stop_fill_long_close.py`.

---

## Scenario 3 — Stop trigger, debit/credit signs, and breach comparison

Two **separate** stop mechanisms exist. Do not conflate them.

### Mechanism A — Exchange STOP_LIMIT (short leg only)

- **Placed by:** `stop_monitor.setup_initial_stop()` or `place-stop` adhoc command
- **Trigger:** Short leg trades at stop price on the exchange
- **Sign:** `BUY_TO_CLOSE` → **debit** (negative in TastyTrade SDK v12+)
- **Formula:** `((short_fill - 0.10) × STOP_PRCNT_C)` rounded to nearest $0.05

### Mechanism B — Phase 1 software breach

- **Runs in:** `stop_monitor/phases.py` → `Phase1InitialStop`
- **Trigger:** `spread_mid >= two_x_short + 0.20`
- **Spread mid:** `short_mid - long_mid` (cost to buy back the spread)
- **Action:** Cancel exchange stop → `replace_with_limit_close()` places **LIMIT** at **live MQTT short mid** (`get_market_mid`, not breach overrides), rounded via `round_spx_option_price()`
- **Chase:** While the breach limit is working, each poll compares streamed short mid to the current limit. If the rounded mid moves (e.g. $9.00 → $9.50), cancel and re-place; if unchanged (e.g. sim stuck at $0.20), leave the order alone

### Testing breach (Scenario 2 extension)

When the live short strike is far below the stop, use **`--simulate-breach`** on `stop-session` or `run-stop-monitor`. Overrides affect **breach detection only**; the limit price comes from the streamer mid.

**Expected:** exchange stop cancelled, limit `BUY_TO_CLOSE` on short placed; check logs for `spread_stop_breach` / `replaced_limit`.

**Offline first:**

```python
# stop_monitor/breach.py
spread_breach_triggered(spread_price, stop_threshold)  # True when spread_price >= threshold
```

Higher spread cost = worse for credit spread holder = breach. A rally in the **long** leg reduces spread cost and should **not** false-trigger.

### Offline verification (no broker)

```powershell
uv run python tests/run_tests.py
uv run python tests/adhoc_integration.py simulate-breach --side C --short-fill 4.0 --long-fill 2.5
```


| Test file                    | What it locks                                   |
| ---------------------------- | ----------------------------------------------- |
| `test_order_prices.py`       | Debit/credit sign per action                    |
| `test_broker_order_paths.py` | Production order paths use correct sign         |
| `test_phase1_breach.py`      | `>=` direction, long-leg offset, threshold edge |
| `test_option_ticks.py`       | SPX $0.05 / $0.10 tick rounding at $3 boundary   |


### Example numbers (short_fill = 1.45, side = C — your Jun 22 position)


| Value             | Calculation                     | Result     |
| ----------------- | ------------------------------- | ---------- |
| `two_x_short`     | `round(1.45 × 2 / 0.05) × 0.05` | 2.90       |
| Exchange stop     | `((1.45 - 0.10) × 2.0)` → $0.05 | 2.70 debit |
| Phase 1 threshold | `two_x_short + 0.20`            | 3.10       |
| Breach when       | `short_mid - long_mid >= 3.10`  | see below  |


### Example numbers (short_fill = 4.00, side = C)


| Value             | Calculation                     | Result                                                                           |
| ----------------- | ------------------------------- | -------------------------------------------------------------------------------- |
| `two_x_short`     | `round(4.00 × 2 / 0.05) × 0.05` | 8.00                                                                             |
| Exchange stop     | `((4.00 - 0.10) × 2.0)` → $0.05 | 7.80 debit                                                                       |
| Phase 1 threshold | `two_x_short + 0.20`            | 8.20                                                                             |
| Breach when       | `short_mid - long_mid >= 8.20`  | e.g. short=10, long=2 → spread 8.00 → no; short=10, long=1.5 → spread 8.50 → yes |


### Live verification limits


| What                     | Can test off-hours?       | How                                                            |
| ------------------------ | ------------------------- | -------------------------------------------------------------- |
| cr/db sign on stop place | Yes                       | `place-stop` or `stop-session` — confirm order accepted        |
| Exchange stop **fill**   | No                        | Requires short leg to trade at stop during market hours        |
| Phase 1 breach fire      | Partially                 | Watch logs when spread mid approaches threshold; or unit tests |
| Mock price injection     | Yes (`--simulate-breach`) | Synthetic MQTT overrides in `MqttPriceCache`                   |


---

## JSON handshake fields


| Field                     | Set by       | Meaning                                                 |
| ------------------------- | ------------ | ------------------------------------------------------- |
| `open_order_id`           | Entry thread | TastyTrade order # — stop_monitor syncs fills from this |
| `status`                  | fill_sync    | `pending_fill` → `open` when paired units fill and both leg prices known |
| `quantity`                | Entry thread | Target contracts ordered                                |
| `filled_quantity`         | fill_sync    | Contracts filled so far (stop size)                     |
| `stop_quantity`           | stop_monitor | Contracts covered by current exchange stop              |
| `open_order.fully_filled` | fill_sync    | Stop syncing entry order when true                      |
| `open_order.last_sync`    | fill_sync    | Last broker poll timestamp                              |


---

## TastyTrade debit / credit reference

TastyTrade SDK v12+: **negative = debit**, **positive = credit**.


| Action          | Use                    | Sign  |
| --------------- | ---------------------- | ----- |
| `SELL_TO_OPEN`  | Open spread for credit | **+** |
| `BUY_TO_OPEN`   | Open long leg          | **−** |
| `BUY_TO_CLOSE`  | Stop / close short leg | **−** |
| `SELL_TO_CLOSE` | Close long leg         | **+** |


All TastyTrade orders go through `brokers/tastytrade_broker.py` → `_signed_order_price()`.

---

## Command reference

### `tests/run_tests.py` — offline unit tests

```powershell
uv run python tests/run_tests.py
```

### `tests/adhoc_integration.py` — broker integration


| Command                      | Purpose                                                    |
| ---------------------------- | ---------------------------------------------------------- |
| `check-env`                  | Validate `.env`                                            |
| `check-mqtt`                 | Mosquitto reachable                                        |
| `check-auth`                 | Broker login                                               |
| `check-prices`               | Fetch SPX mid (streamer required)                          |
| `check-all`                  | All checks above                                           |
| `place-trade`                | Place one spread + write JSON (market hours)               |
| `place-stop`                 | Stop on short leg at explicit price                        |
| `seed-stop`                  | JSON for existing position (no new entry)                  |
| `run-stop-monitor`           | Monitor active JSONs (`--poll` default 5s; streamer separate) |
| `stop-session --seed`        | Scenario 2 Step 1: seed + streamer + stop_monitor + MQTT   |
| `stop-session --simulate-breach` | Scenario 2 Step 2: breach sim (reuse Step 1 JSON)      |
| `partial-fill-session`       | Scenario 4 entry: `--simulate-partial-fill`, `--partial-step 1/2`, `--partial-qty N` |
| `test-long-close`            | Scenario 5a: long `SELL_TO_CLOSE` only (`--quantity` overrides JSON; omit for JSON default) |
| `stop-fill-session`          | Scenario 5b: cancel live short stop → sim stop filled → long close + JSON to `closed/` |
| `integration-session`        | Delegate to `run.py --integration-session`                 |
| `simulate-breach`            | Offline Phase 1 breach table                               |
| `full-smoke --paper --place` | Connectivity + optional trade + monitor                    |


### `run.py` — production launcher flags


| Flag                           | Purpose                                                   |
| ------------------------------ | --------------------------------------------------------- |
| `--integration-session`        | Full 5-min test: streamer + stop_monitor + tranche + MQTT |
| `--expiry YYYY-MM-DD`          | Target expiry for integration session                     |
| `--duration SECONDS`           | MQTT collection after tranche (default 300)               |
| `--integration-tranche`        | Tranche only; no stop_monitor                             |
| `--force --tranche-now --once` | Manual off-hours tranche                                  |
| `--no-stop-monitor`            | Skip stop_monitor subprocess                              |
| `--lot NAME`                   | Tranche lot label                                         |
| `--paper`                      | Paper trading session                                     |


---

## Useful one-liners

**Jul 22 CCS test trade (market hours):**

```powershell
uv run python tests/adhoc_integration.py place-trade --side C --expiry 2026-07-22 --short-strike 7525 --long-strike 7550 --quantity 1
```

**Stop on short call at $3 debit:**

```powershell
uv run python tests/adhoc_integration.py place-stop --side C --short-strike 7635 --quantity 5 --stop 3.0 --expiry 2026-06-22
```

**Paper first-time smoke (market hours):**

```powershell
uv run python tests/adhoc_integration.py check-all --paper
uv run python tests/adhoc_integration.py place-trade --side P --paper
uv run python tests/adhoc_integration.py run-stop-monitor --seconds 120 --paper
```

---

## Troubleshooting


| Symptom                        | Likely cause                                   | Fix                                                                                      |
| ------------------------------ | ---------------------------------------------- | ---------------------------------------------------------------------------------------- |
| MQTT counts all zero           | Mosquitto down or streamer not running         | `check-mqtt`; use `stop-session` or `--integration-session` (starts streamer)            |
| No orders on TastyTrade        | Auth / market closed / strike selection failed | `check-auth`; check tranche logs in `meic0dte/logs/`                                     |
| `Could not get SPX from MQTT`  | Streamer not running or Mosquitto down         | Start `run.py` (starts streamer) or run `streaming/publish_tastytrade.py` before tranche |
| `cant_buy_for_credit` on stop  | Stop priced as credit instead of debit         | Fixed in `_signed_order_price()` — re-run unit tests                                     |
| stop_monitor idle              | No `trades/active/*.json`                      | Use `seed-stop` or wait for fill                                                         |
| Duplicate stops                | Multiple JSON files for same position          | Remove stale files from `trades/active/`                                                 |
| Streamer exits at 3 PM         | Normal production behavior                     | `MEIC_INTEGRATION=1` disables 3 PM shutdown                                              |
| Event loop closed (TastyTrade) | Async session torn down                        | Persistent loop in `tastytrade_broker.py`                                                |


---

## Files produced during tests


| Path                             | When                                           |
| -------------------------------- | ---------------------------------------------- |
| `trades/integration_report.json` | Integration session / integration tranche      |
| `trades/active/*.json`           | Fill or `seed-stop`                            |
| `trades/closed/*.json`           | Position closed by stop_monitor                |
| `optsymbols.json`                | Streamer symbol registry (updated on register) |
| `*_put.log` / `*_call.log`       | Per-tranche thread logs                        |


---

## Suggested test order

1. `uv run python tests/run_tests.py` — includes `test_fill_sync.py`, `test_partial_fill_stop.py`, `test_stop_fill_long_close.py`, `test_phase1_breach.py`
2. `uv run python tests/adhoc_integration.py check-all`
3. **Scenario 1** — `--integration-session` (off-hours tranche + MQTT)
4. **Scenario 2** — `stop-session --seed` then optional `--simulate-breach` (existing position stop + breach)
5. **Scenario 4** — `partial-fill-session --simulate-partial-fill --partial-step 1 --partial-qty 2` then `--partial-step 2` (paired partial entry → stop resize)
6. **Scenario 5** — `test-long-close --quantity 1` (isolated long close) or `stop-fill-session` (full stop-out chain)
7. **Scenario 3** — `simulate-breach` + review breach unit tests

---

## Open questions / future work

- **`seed-from-order`** — adhoc command: only `open_order_id`, broker fills JSON (handshake without manual fills)
- **Integration report for stops** — append stop order IDs and resize events
- **Automated pass/fail** — assert minimum MQTT counts and `order_id` presence in CI
- **Decouple MQTT breach from broker sync** — fast (≤1s) spread-mid breach vs ~30s entry/stop status polls (see Stop monitor architecture)

