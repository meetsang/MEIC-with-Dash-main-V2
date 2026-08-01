# V2 Appendix — Gaps, Current State, and Fill Plan

**Companion to:** [`V2_MODULAR_REWRITE.md`](V2_MODULAR_REWRITE.md)  
**Date:** Jun 24, 2026  
**Status:** Analysis only — no code changes in this document  
**Purpose:** List everything V2 assumes that V1 does not yet provide, and a practical plan to close each gap.

---

## How to read this document

Each gap entry follows the same structure:

| Field | Meaning |
|-------|---------|
| **V2 reference** | Where the main doc expects this |
| **V1 today** | What actually exists in `MEIC-with-Dash-main` |
| **Severity** | Blocker / High / Medium / Low for MEIC-only V2 MVP |
| **Fill plan** | Concrete steps to close the gap in a V2 repo |
| **Acceptance** | How we know it is done |

Gaps are grouped by building block. Order within each group is suggested implementation sequence.

---

## Summary matrix

| # | Gap | Severity (MEIC MVP) | Effort |
|---|-----|---------------------|--------|
| G1 | Strategy layer not wired | **Blocker** | Medium |
| G2 | Stop block coupled to MEIC config | **Blocker** | Medium |
| G3 | Entry block not extracted | **Blocker** | Medium |
| G4 | State schema MEIC-specific | High | Medium |
| G5 | Manual spread not in strategy model | High | Low–Medium |
| G6 | Streamer staleness guard missing | High | Medium |
| G7 | Streamer symbol removal missing | Low | Low |
| G8 | Orphan `closing` recovery missing | Medium | Low |
| G9 | Entry config validation missing | Medium | Low |
| G10 | Debit spread / breach inversion | Low (defer) | High |
| G11 | Iron Fly / multi-leg entry | Low (defer) | High |
| G12 | Multi-ticker (NQ/MNQ) | Low (defer) | Medium |
| G13 | Schwab dual architecture | Medium (scope) | High or cut |
| G14 | Dashboard schema migration | High | Medium |
| G15 | V1 parity items newer than Jun 21 doc | Medium | Low (port + test) |
| G16 | MQTT removal (future) | N/A for launch | High |

---

## Part A — Strategy & orchestration gaps

### G1 — Strategy layer not wired to launcher

**V2 reference:** Part 3 (Strategy Layer), Part 4 (Orchestrator), Part 6 (`strategies.yaml`)  
**V1 today:**

- `strategies/base.py`, `strategies/loader.py`, `config/strategies.yaml` exist.
- `MEICStrategy` is a stub — logs “delegates to run.py”, does not run entry or stop.
- `run.py` hardcodes `TRANCHES`; never calls `load_strategies()`.
- Tranche pause uses `meic0dte/trades/pause_tranches.json` globally.

**Severity:** Blocker for multi-strategy V2; Blocker for YAML enable/disable even with one strategy.

**Fill plan:**

1. Define `StrategyBase` with the interface from V2 Part 3.1 (`name`, `instrument`, `entry_block()`, `stop_profile()`, `schedule()`, `pre_entry_check()`).
2. Implement `MEICStrategy` for real — wrap existing entry/stop config, not a log stub.
3. Replace `run.py` tranche loop with `Orchestrator`:
   - Load enabled strategies from `strategies.yaml`.
   - For each strategy, iterate `schedule()` slots.
   - Fire `entry_block().execute(lot, side)` in window; mark slot fired (persist to avoid double-fire on restart).
4. Keep subprocess model initially (`app_main.py` equivalent) if desired; orchestrator only needs a stable callable boundary.
5. Map `pause_tranches.json` keys to `{strategy}:{lot}` or keep global pause as orchestrator-level gate.

**Acceptance:**

- Disable MEIC in YAML → no tranches fire.
- Enable MEIC → identical tranche times and behavior to V1.
- Integration test: `test_multi_strategy` deferred until second strategy exists; MEIC-only orchestrator test passes first.

---

### G5 — Manual spread outside strategy model

**V2 reference:** Part 6 folder layout (`trades/active/` per strategy); not explicitly documented.  
**V1 today:**

- `manual_spread/` is a parallel module with its own `trades/active/`, `trades/commands/`.
- Reuses `meic0dte/open/*`, `stop_monitor/state.py`, `stop_monitor/fill_sync.py`.
- Strategy string `MANUAL_SPREAD`; lots `ms-1`, `ms-2`, …
- Dashboard routes in `dashboard/manual_spread_handlers.py`.

**Severity:** High — any V2 repo must decide this on day one or manual trading breaks.

**Fill plan (recommended):**

1. Treat manual spread as **`ManualSpreadStrategy`** — not scheduled, but implements same `StopProfile` as MEIC credit verticals.
2. Entry path: dashboard → `ManualSpreadEntry` (thin wrapper over `CreditSpreadEntry` with scan params from `manual_spread/config.py`).
3. Unify trade roots: `trades/active/{strategy}/` or keep flat filenames with strategy prefix (already `MANUAL_SPREAD_SPX_*`).
4. `StopMonitorSupervisor` watches all `*/trades/active/*.json` (already does for MEIC + manual paths via env vars).
5. Dashboard: add strategy filter column; API unchanged if filenames stay compatible.

**Acceptance:**

- Dashboard manual PCS/CCS → JSON in active → stop placed → same lifecycle as MEIC leg.
- V2 doc updated (separate note) to list `ManualSpreadStrategy` as first-class.

---

### G13 — Schwab dual architecture

**V2 reference:** Assumes TastyTrade path throughout.  
**V1 today:**

- Full Schwab stack: `meic0dte/order/`, `meic0dte/close/`, `streaming/publish.py`, `auth/`.
- `BrokerBase` / `TastyTradeBroker` used on TT path only; Schwab bypasses broker abstraction.

**Severity:** Medium — scope decision, not a MEIC+TT blocker.

**Fill plan (pick one):**

| Option | When | Work |
|--------|------|------|
| **A — TT-only V2** | Production is TT | Document Schwab as V1-only legacy; do not port |
| **B — Port Schwab to BrokerBase** | Still need Schwab | Implement `SchwabBroker(BrokerBase)`; retire `meic0dte/order/` |
| **C — Shim** | Transition period | V2 launches TT-only; Schwab stays on V1 repo |

**Recommendation:** **A** for V2 MVP — matches doc’s TT focus and reduces rewrite surface.

**Acceptance:**

- V2 README states broker support explicitly.
- No dead imports from `meic0dte.app.vertical` (Schwab) in V2 tree.

---

## Part B — Stop block gaps

### G2 — Stop block not strategy-agnostic

**V2 reference:** Part 2.1 (`StopProfile`, `PhaseRule`)  
**V1 today:**

- `stop_monitor/monitor.py` imports `meic0dte.app.config` (`STOP_PRCNT_C/P`, `LIMIT_OFFSET`, `STRK_CHK_MIN`).
- `stop_monitor/phases.py` imports MEIC config + `central_time` from `meic0dte.app.utilities`.
- Phases are classes (`Phase1InitialStop`, etc.) registered via `default_phases()` — not data-driven `PhaseRule`s.

**Severity:** Blocker for second strategy; Medium refactor for MEIC-only V2 if MEIC profile lives in `strategies/meic/`.

**Fill plan:**

1. **Extract `StopProfile` dataclass** in `blocks/stop/stop_profile.py`:
   - `initial_stop_calc`, `phases: list[PhaseRule]`, `breach_calc`, `breach_condition`, `long_close_delay_sec`, proximity settings.
2. **Move MEIC numbers** to `strategies/meic/config.py` and `strategies/meic/stop_profile.py`.
3. **Refactor `StopMonitor` → `StopBlock`**:
   - Constructor takes `stop_profile: StopProfile`.
   - Phase loop: sort by `priority`, evaluate `condition(state, prices)`, run `action(stop_block)`.
4. **Port existing phase logic** verbatim into MEIC `PhaseRule` factories — behavior change forbidden in step 1.
5. Remove all `import meic0dte` from `blocks/stop/*`.

**Acceptance:**

- V1 vs V2 side-by-side test: same mock prices → same breach/stop/phase decisions.
- `grep meic0dte blocks/stop/` returns zero.

---

### G4 — State schema MEIC-specific

**V2 reference:** Part 2.1 State Schema (V2)  
**V1 today:**

- JSON uses `two_x_short`, `short_stoplmt_replaced`, `phases.phase1_active`, `entry.two_x_net_credit`, etc.
- No `spread_type`, `stop_profile`, `strategy_version`, leg-level `action`.
- `stop_monitor/state.py` `create_new_state()` / `create_pending_state()` hardcode MEIC shape.

**Severity:** High — dashboard and stop block both depend on current fields.

**Fill plan:**

1. **Define V2 schema** as doc specifies; add **`strategy_data`** dict for MEIC-specific fields instead of deleting V1 fields on day one:
   ```json
   "strategy_data": {
     "meic": {
       "two_x_short": 1.55,
       "short_stoplmt_replaced": false
     }
   }
   ```
2. **Compatibility shim** (optional): reader accepts V1 flat fields and normalizes to V2 on load.
3. Update `create_pending_state` / `create_new_state` to write V2 shape.
4. Migrate dashboard templates/JS to read both during transition, then drop V1.

**Acceptance:**

- New trades written in V2 schema.
- Existing V1 JSON loads and runs (shim) or one-time migration script documented.
- Unit tests: round-trip V2 schema; MEIC fields accessible via `strategy_data.meic`.

---

### G8 — Orphan `closing` recovery

**V2 reference:** Part 5.1 — “if `closing` with no `long_close_order_id`, re-place on load”  
**V1 today:**

- `handle_stop_order_update` sets `status=closing` with no long order; first long order placed after `LONG_CLOSE_DELAY_SEC` via `_chase_long_close`.
- `_on_load` does not check for `closing` + missing `long_close_order_id` after delay elapsed.
- Process crash during 30s wait or during chase → trade can sit in `closing` with no working long order.

**Severity:** Medium — edge case but real on restart.

**Fill plan:**

1. In `StopBlock._on_load()`:
   - If `status == 'closing'` and `short_closed_at` older than `long_close_delay_sec` and no `long_close_order_id` → call `_chase_long_close()` (or schedule immediate chase).
   - If `long_close_order_id` set but broker shows working → resume chase loop.
2. Add unit test: load JSON `closing`, no long oid, `short_closed_at` 60s ago → places long close.

**Acceptance:**

- Restart stop monitor mid-close → long leg order re-placed or chase resumed without manual intervention.

---

### G10 — Debit spread / breach inversion (defer)

**V2 reference:** Part 2.4, Part 2.1 `breach_calc` / `breach_condition`  
**V1 today:**

- `stop_monitor/breach.py`: `spread_mark_price = short_mid - long_mid`; breach when `spread >= threshold`.
- Credit spreads only; no `spread_type` in JSON.

**Severity:** Low for MEIC MVP — explicitly deferred in V2 Part 10 #8.

**Fill plan (when needed):**

1. Add `spread_type: credit | debit` to trade state.
2. `StopProfile.breach_calc` and `breach_condition` supplied by strategy (already in V2 design).
3. Implement `DebitSpreadEntry` block; flip leg actions and NET_DEBIT order sign (TT SDK convention).
4. Tests: `test_breach.py` credit + debit directions.

**Acceptance:**

- Deferred until first debit strategy is defined; interface stub in V2 repo is enough for MVP.

---

### G15 — V1 behavior newer than Jun 21 V2 doc

**V2 reference:** Part 1.1, Part 5.1 (partial fills, long close timing)  
**V1 today (post–Jun 21 fixes):**

| Behavior | V1 implementation |
|----------|-------------------|
| Entry fill sync 3s | `PENDING_FILL_SYNC_INTERVAL_SEC = 3`, force sync on dashboard modify |
| Partial entry fill | `fully_filled` gate; stop for `filled_quantity` only; resize on remainder |
| Stop full fill before long close | `stop_order_fully_filled()` |
| Long close: 30s delay before first order | `handle_stop_order_update` no longer calls `_close_long_leg()` immediately |
| Long chase step-down | `_compute_long_close_limit` — 5¢/10¢ tick step when mid unchanged |
| Strike overlap shift | `common/strike_guard.py` — CCS −$5, PCS +$5 |

**Severity:** Medium — V2 port must include these or regress.

**Fill plan:**

1. Port `fill_sync.py`, long-close chase, strike guard into V2 blocks with **existing unit tests copied first**.
2. Update V2 main doc §1.1 long-close bullet to: “30s delay **before** first long order, then 3s chase.”
3. Add V2 regression tests mirroring: `test_fill_sync.py`, `test_partial_fill_stop.py`, `test_stop_fill_long_close.py`, `test_long_close_chase.py`, `test_strike_guard.py`.

**Acceptance:**

- All listed V1 tests pass against V2 block code without logic changes.

---

## Part C — Entry block gaps

### G3 — Credit spread entry not extracted

**V2 reference:** Part 2.3 (`CreditSpreadEntry`, `CreditEntryConfig`)  
**V1 today:**

- Logic in `meic0dte/open/spread_scan.py`, `open_spread_tt.py`, `meic0dte/app/config.py`.
- `manual_spread/entry.py` duplicates handoff patterns.
- Scan uses MQTT via broker REST fallback for chain; credit eval uses streamed mids after symbol registration.

**Severity:** Blocker for composability; Medium extract for MEIC-only V2.

**Fill plan:**

1. Create `blocks/entry/credit_spread.py` — move flow from V2 Part 2.3 diagram verbatim.
2. Create `blocks/entry/entry_config.py` — map from `meic0dte/app/config.py` defaults.
3. Create `blocks/entry/strike_scanner.py` — extract from `spread_scan.py` (include overlap shift from `strike_guard.py`).
4. `open_spread_tt.py` becomes thin: `CreditSpreadEntry(config).execute(lot, side)`.
5. Keep fire-and-forget: `fill_wait_max` then hand off JSON to stop block.

**Acceptance:**

- One tranche open produces identical JSON + broker order vs V1 (paper comparison test).
- `CreditEntryConfig(credit_min=2.0)` changes behavior without editing block code.

---

### G9 — Entry config validation

**V2 reference:** Part 5.4 — `CreditEntryConfig.__post_init__()`  
**V1 today:** Invalid config fails silently at runtime (“no suitable credit”).

**Severity:** Medium.

**Fill plan:**

1. Add dataclass validation: `credit_min > 0`, `credit_max >= credit_min`, width/OTM ranges ordered, `quantity >= 1`.
2. Orchestrator validates on strategy load — fail fast at startup with clear error.
3. Unit tests in `test_entry_config.py`.

**Acceptance:**

- `credit_min=2.0, credit_max=1.0` raises `ValueError` at load time.

---

## Part D — Streamer block gaps

### G6 — Streamer staleness guard

**V2 reference:** Part 2.2 Staleness Guard, Part 5.1 stale price freeze, Part 5.3  
**V1 today:**

- Streamer publishes prices to MQTT; no heartbeat with `last_spx_price_ts`.
- Stop breach loop does **not** freeze on stale MQTT (GAP-09 identified, not implemented).
- `meic0dte/trades/heartbeat.json` is stop_monitor supervisor heartbeat, not streamer health.

**Severity:** High for production safety; called out as “most dangerous failure mode” in V2.

**Fill plan:**

1. **Streamer** publishes `streaming/health.json` or MQTT retained topic every 5s:
   - `last_spx_price_ts`, `symbols_subscribed`, `msgs_per_min`, `status`.
2. **Stop block** reads health; if SPX price age > 30s during market hours:
   - Skip breach checks and phase triggers that depend on MQTT.
   - Log CRITICAL; optional Slack.
   - Exchange stop at broker remains active (doc’s intent).
3. **Orchestrator** restarts streamer subprocess on exit (V1 `run.py` already restarts — formalize in V2).

**Acceptance:**

- Integration test: freeze MQTT SPX updates → no breach limit placed for N seconds.
- Dashboard shows streamer health indicator (optional Phase 2).

---

### G7 — Symbol removal from streamer

**V2 reference:** Part 2.2 `remove_symbols()`  
**V1 today:** `optsymbols.json` append-only; EOD `session_cleanup` clears entire file.

**Severity:** Low — EOD clear works; removal helps long-running sessions with many closed trades.

**Fill plan:**

1. Add `remove_symbols(symbols)` to streamer — rewrite `optsymbols.json` minus closed legs.
2. Call from `StopBlock._finalize_close()` after archive.
3. Dedup on write (already exists).

**Acceptance:**

- Close trade → legs removed from optsymbols within one streamer poll cycle.

---

## Part E — Dashboard & ops gaps

### G14 — Dashboard schema migration

**V2 reference:** Part 6 dashboard; Part 7.1 “mostly unchanged — add strategy filter”  
**V1 today:** `dashboard/server.py` reads V1 JSON fields; SQLite history; manual spread routes separate.

**Severity:** High if V2 schema (G4) ships without dashboard update.

**Fill plan:**

1. **Phase 1:** Compatibility reader in dashboard — supports V1 + V2 JSON.
2. **Phase 2:** Strategy column/filter; unify MEIC + manual grid.
3. **Phase 3:** Drop V1 field reads after migration window.

**Acceptance:**

- Dashboard shows open trades, stops, P&L from V2 JSON without broker calls.

---

## Part F — Future strategy gaps (explicitly defer)

### G11 — Iron Fly

**V2 reference:** `strategies.yaml` Iron_Fly entry; Part 3.3 implied multi-leg  
**V1 today:** `strategies/iron_fly/strategy.py` stub; portfolio-level phases named in YAML only.

**Fill plan:** New entry block (4 legs, middle body, different stop model). Not composable from `CreditSpreadEntry`. Defer until MEIC V2 stable.

---

### G12 — Multi-ticker (NQ/MNQ)

**V2 reference:** Part 7.3 defer; `instrument` field in V2 schema  
**V1 today:** `tests/test_stream_nq_futures.py` proves stream works; `_get_symbols()` in streamer mangles non-SPX symbols; entry/stop hardcoded SPX.

**Fill plan:**

1. `instrument_config` per strategy: index symbol, option symbol format, tick size.
2. Streamer: front-month resolution per product (like NQ test).
3. Entry scanner parameterized by strike step and symbol builder.

**Acceptance:** Deferred; SPX-only V2 MVP is valid.

---

## Part G — MQTT: removal vs keep (answers FAQ)

### Does V2 talk about removing MQTT?

**Yes, but only as a future option — not for launch.**

| Location | What it says |
|----------|--------------|
| **Part 7.3 — What Can Be Deferred** | “Remove MQTT (Goal 3 from V1) — MQTT works fine. V2 can start with MQTT and migrate to internal queues later.” |
| **Part 10 — Decision #1** | Option A: keep MQTT for V2 launch, remove later. Option B: remove from day 1. **Recommendation: A.** |

Everywhere else, V2 **depends on MQTT**: breach detection, long chase mids, entry scan pricing, dashboard live quotes, kill switch topic.

### Why mention removal at all?

It comes from **V1 “Goal 3”** — an earlier idea to eliminate Mosquitto as a middleman and use in-process queues (Streamer → consumers directly). Reasons people consider it:

| Motivation | Tradeoff |
|------------|----------|
| Fewer moving parts (no Mosquitto daemon) | Lose process isolation between streamer and stop/entry |
| Lower latency | MQTT local is already sub-ms; gain is small |
| Simpler deployment | Must replace kill switch, dynamic subscribe, dashboard fan-out |

**Recommendation for V2 (unchanged from main doc):** Keep MQTT for launch. Revisit internal queues only after V2 MEIC parity on paper — it is an optimization, not a prerequisite for modularity.

### G16 — MQTT removal (if pursued later)

**Fill plan:**

1. Replace `MqttPriceCache` with thread-safe in-memory `PriceFeed` + optional multiprocessing queue from streamer subprocess.
2. Reimplement kill switch as shared file or queue event.
3. Dashboard: WebSocket from orchestrator or read shared cache via HTTP — today it subscribes MQTT directly.

**Acceptance:** Not required for V2 MVP; document as V2.1+ epic.

---

## Part H — Suggested implementation phases

### Phase 0 — Parity lock (1 week)

- Copy unit tests listed in G15 into V2 repo skeleton.
- No refactor — prove test harness runs.

### Phase 1 — MEIC MVP V2 (3–4 weeks)

- `blocks/stop/` port with `StopProfile` (MEIC only).
- `blocks/entry/credit_spread.py` extract.
- `blocks/streamer/` port + **G6 staleness** (minimum viable health file).
- `strategies/meic/` + **G1 orchestrator** (single strategy).
- **G5 manual spread** as second strategy class or documented extension.
- **G4 schema** with `strategy_data.meic` shim.
- Dashboard **G14 Phase 1** compat reader.

**Exit criteria:** Paper run full day; entry → stop → breach → long close matches V1 logs.

### Phase 2 — Composability proof (2 weeks)

- Second credit strategy (e.g. Wide Wing from V2 doc §3.3).
- **G9 config validation** at load.
- **G8 orphan closing** recovery.
- **G7 symbol removal**.

### Phase 3 — Deferred epics

- G10 debit entry + breach inversion.
- G11 Iron Fly.
- G12 multi-ticker.
- G13 Schwab port (if needed).
- G16 MQTT removal.

---

## Part I — Test checklist (map gaps → tests)

| Gap | Test to add in V2 |
|-----|-------------------|
| G1 | `test_orchestrator_fires_tranche_once` |
| G2 | `test_meic_stop_profile_parity` (vs V1 golden outputs) |
| G3 | `test_credit_entry_handshake_json` |
| G4 | `test_state_v2_schema_roundtrip` |
| G5 | `test_manual_spread_lifecycle` |
| G6 | `test_breach_frozen_on_stale_streamer` |
| G8 | `test_closing_orphan_recovery_on_load` |
| G9 | `test_entry_config_validation` |
| G15 | Port all V1 tests listed in G15 table verbatim |

---

## Part J — Open decisions (for appendix readers)

These should be resolved before Phase 1 coding; defaults suggested:

| Question | Suggested default |
|----------|-------------------|
| V2 repo name / location | New repo; V1 frozen when V2 paper-parity achieved | ~~~ New repo MEIC-with-Dash-main-V2
| Manual spread | First-class `ManualSpreadStrategy` |
| Schwab | Out of scope for V2 MVP | ~~~ this is fine, but model the broker connection as a separate layer, easy to switch.
| MQTT | Keep (Decision #1 = A) | ~~~ No need for removal
| State migration | Shim on read; V2 write format for new trades |
| Long close timing | 30s before first long order (current V1, not Jun 21 doc wording) | ~~~Yes use this

---

## Part K — State migration: what “shim on read” means

V2 introduces a **new trade JSON shape** (e.g. `strategy_version`, `spread_type`, `stop_profile`, MEIC fields nested under `strategy_data.meic`). V1 trades on disk use the **old flat shape** (`two_x_short`, `phases.short_stoplmt_replaced` at the top level, etc.).

**“V2 write format for new trades”** — any trade opened after V2 launches is saved in the new schema only.

**“Shim on read”** — a thin compatibility layer in `load_state()` (or equivalent) that:

1. Reads the JSON file from disk (could be V1 or V2 format).
2. If it looks like V1 (e.g. no `strategy_version`, flat `two_x_short`), **normalizes in memory** to the V2 structure the rest of the code expects.
3. Runs stop/entry/dashboard logic against the normalized object — **no manual file edit required**.

Example (conceptual):

```python
def load_state(path):
    raw = json.load(open(path))
    if raw.get("strategy_version") is None:
        raw = _v1_to_v2_normalize(raw)  # shim on read
    return raw
```

**What shim does *not* mean:**

- It does **not** require rewriting every old file on disk immediately.
- It does **not** mean V1 repo reads V2 files (V1 stays frozen).

**Optional later:** a one-time migration script that rewrites `trades/history/` to V2 format on disk — only if you want cleaner archives; not required for MVP.

**Alternative (no shim):** V2 only accepts new-format JSON; old V1 files are never loaded in V2 (clean break, but no recovery of in-flight V1 trades in the V2 repo).

---

## Part L — Pre-repo decisions (answer inline)

Fill in your choice after each `→` (or add a short note). V1 repo `MEIC-with-Dash-main` stays untouched.

### L1 — Repo bootstrap (first commit contents)

How should `MEIC-with-Dash-main-V2` start?

- **A)** Clean skeleton — folder layout, README, `strategies.yaml`, empty `blocks/`, copy **tests only** from V1; port code file-by-file  
- **B)** Full **copy of V1** into V2, then refactor in place  
- **C)** Skeleton + copy **only Phase 1 files** (`brokers/`, `common/`, key tests, etc.)

→ **Your answer:**
A
---

### L2 — Git history

- **A)** Fresh `git init` in V2 (no V1 history)  
- **B)** Copy V1 git history as starting point  

→ **Your answer:**
I do not have git configured yet for this repo, its fine to skip.
---

### L3 — Repo location

Confirm sibling path: `c:\Users\meets\Downloads\MEIC\SPX\MEIC-with-Dash-main-V2`

→ **Your answer:** (yes / different path: ___)
yes
---

### L4 — Trade directory layout

- **A)** V1-style split: `meic0dte/trades/` + `manual_spread/trades/` (lower dashboard risk)  
- **B)** Unified: `trades/active/{strategy}/` (cleaner long-term)  

→ **Your answer:**
B
---

### L5 — Day-one runnable goal (first commit)

- **A)** Structure + docs + tests pass (no live trading yet)  
- **B)** Enough copied V1 to run paper trading like today  
- **C)** Structure + docs only; port in a follow-up session  

→ **Your answer:**
Able to Production run it.
---

### L6 — `.env` / secrets in V2

- **A)** `.env.example` only — you copy secrets manually  
- **B)** Copy `.env` from V1 into V2 (same credentials)  
- **C)** Symlink V1 `.env` from V2  

→ **Your answer:**
B
---

### L7 — Dashboard in Phase 1

- **A)** Port **full** dashboard from V1 (compat reader for V1/V2 JSON)  
- **B)** Minimal dashboard first (read active JSON + MQTT only)  
- **C)** No dashboard in first commit; add in Phase 1 week 2  

→ **Your answer:**
A
---

### L8 — `strategies.yaml` on day one

- **A)** MEIC + Manual only  
- **B)** MEIC + Manual + disabled **Iron Fly** stub (like V1 YAML)  

→ **Your answer:**
A
---

### L9 — State migration (related to Part K)

- **A)** **Shim on read** — V2 code loads V1-shaped JSON and normalizes in memory; new trades written as V2  
- **B)** **Clean break** — V2 only understands new JSON; no V1 file loading in V2  

→ **Your answer:**
B
---

### L10 — Anything else before repo create?

→ **Your notes:**

---

*This appendix should be read together with [`V2_MODULAR_REWRITE.md`](V2_MODULAR_REWRITE.md). Update both when gaps close or scope changes.*
