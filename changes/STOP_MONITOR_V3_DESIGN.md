# Stop Monitor V3 — Design Document

**Status:** **Implemented** (2026-07-05) — V2.9 + full V3 code shipped locally; **live C1/C2 validation pending** first market session. V2 rollback retained per §10.  
**Date:** 2026-07-04 design; 2026-07-05 implementation pass  
**Motivation:** [LIVE_SESSION_2026-07-02.md](LIVE_SESSION_2026-07-02.md) (Kill Selected on 3 MEIC CCS ~14:40 CT felt sequential)  
**Related:** [GAP_ANALYSIS.md](GAP_ANALYSIS.md) GAP-22, [STALE_PENDING_TRADE_JSON.md](STALE_PENDING_TRADE_JSON.md) Change 2 (spread kill), [LIVE_SESSION_2026-06-26.md](LIVE_SESSION_2026-06-26.md) (ms-50 long-chase vs spread-close)

---

## 1. Goals

1. **Unified exit model** — one stop-monitor subsystem watches each open spread for **three conditions** and dispatches the correct close pipeline (operator consensus from Jul 2 notes).
2. **Round-robin supervisor** — replace *N idle per-trade poll threads* with **one fast scan loop** over all open trades; **spin out worker threads only** when a condition fires or async close work is needed.
3. **Parallel broker I/O across trades** — eliminate the Jul 2 pattern where four manual kills queue behind a **single asyncio event loop** and inline blocking handlers, so independent trades can make concurrent progress at TastyTrade (subject to API limits and safety rules).

**Non-goals for V3 (explicit):**

- Rewriting entry monitor, dashboard, or MQTT streamer (except documented touch points).
- Changing software breach threshold math, exchange stop placement math, or phase-2/3 upgrade rules (unless a challenge below forces it).
- Schwab broker path (TastyTrade is the target runtime per `use_thin_tranches()`).

**Future-position abstraction (naming only — no futures code in V3):**

V3 production target is **SPX/SPXW option credit spreads** (MEIC + manual). However, supervisor, worker, and handler names should **avoid MEIC/credit-spread-only assumptions**. Design for:

- Debit spreads and multi-leg option structures
- Single-leg options
- Futures positions (METF and beyond)

`StopSupervisor`, `TradeSlot`, `ExitWorkerPool`, and `BrokerLane` should remain **instrument-agnostic**; product-specific logic lives in `StopProfile`, `ExitHandler`, and phase plugins — not in the scan loop.

---

## 2. Problem statement (Jul 2, 2026)

Operator selected **three MEIC call spreads** and clicked **Kill Selected** (~14:40 CT). Four call JSONs show `manual_close` in the same window.

| Observation | Evidence |
|-------------|----------|
| Stop cancels landed within **3s** (parallel command pickup) | `stop_history` timestamps 14:40:14–17 |
| Final closes in **two waves** (~52s and ~2 min) | `close.timestamp` on trade JSONs |
| Felt **sequential** to operator | Session notes |

**Root causes (verified in code today):**

| # | Cause | Location |
|---|--------|----------|
| R1 | **Manual kill runs inline** in `_poll_once()` — no `_threaded_*` wrapper | `blocks/stop/monitor.py` `_check_dashboard_commands()` → `replace_with_spread_close()` |
| R2 | **All monitor threads share one `TastyTradeBroker`** with **one asyncio loop**; `_run()` uses `run_coroutine_threadsafe` + blocking `future.result()` | `brokers/tastytrade_broker.py` |
| R3 | **N per-trade threads** mostly sleep 3s (`FAST_INTERVAL`) — inefficient but not the main Jul 2 bottleneck | `blocks/stop/runner.py`, `monitor.py` |
| R4 | **Fill recording gap** on spread close (`long_close_price: null`) | Trade JSON + `_apply_spread_close_fill()` not receiving per-leg prices from broker |

---

## 3. Current architecture (as-is)

### 3.1 Process layout

```
run.py (launcher)
  ├── streaming/publish_tastytrade.py   (MQTT prices)
  ├── blocks/stop/run.py                (stop_monitor subprocess)
  │     └── MonitorRunner
  │           └── 1× StopMonitor thread per open trade JSON
  ├── blocks/entry/runner.py            (MEIC + manual entry workers)
  └── dashboard/server.py               (separate process when started)
```

**Important:** `dashboard` and `stop_monitor` are **different OS processes**. Kill Selected writes `trades/commands/{file}.close.json`; it does **not** call the stop_monitor broker directly. Parallelization inside stop_monitor **does not** affect dashboard HTTP latency (milliseconds).

### 3.2 MonitorRunner (per-trade threads)

| Behavior | Code fact |
|----------|-----------|
| Scans `trades/active/` (MEIC + manual) every **3s** | `runner.run_forever()` |
| Starts thread only if `status == open` and full fill | `runner.add()` |
| One `StopMonitor` instance per JSON path | `MonitorRunner._handles` |
| Thread exits when `monitor.run()` sees `status == closed` | `monitor.py` ~128–129 |
| Restart up to 10× if thread dies while still open | `_supervise()` |

**Count:** Not 6×12 = 72. Only **entered, open, fully filled** legs get threads (typically 0–12 MEIC + manual on a busy day).

### 3.3 StopMonitor poll loop (`_poll_once`, every ~3s)

Current order of checks (same thread, blocking where noted):

1. Dashboard **killswitch** or **per-trade `.close.json`** → `replace_with_spread_close()` **inline** (blocks)
2. Stop multiplier update command
3. Working **spread close** poll → `_poll_spread_close()` (broker call, blocks)
4. If `open`: ensure exchange stop placed
5. If `closing`: spread close poll OR schedule **`_threaded_long_chase`** after `long_close_delay_sec` (**30s**)
6. If `open`: MQTT breach watch; every **10s** broker REST reconcile (`SLOW_INTERVAL`)
7. Phase loop → on breach, spawn **`_threaded_phase_execute`** (background)

**Three exit paths exist today but are asymmetric:**

| Path | Trigger | Handler | Threading |
|------|---------|---------|-----------|
| A — Software breach | MQTT spread ≥ threshold | `replace_with_limit_close()` via `Phase1InitialStop` | Background (`_threaded_phase_execute`) |
| B — Exchange stop filled | Alert queue or REST sync | `handle_stop_order_update()` → `closing` → long chase | Long chase background after 30s |
| C — Manual kill | `.close.json` / killswitch | `replace_with_spread_close()` | **Inline (gap)** |

### 3.4 TastyTrade broker

| Fact | Implication |
|------|-------------|
| Single `asyncio` event loop in dedicated thread | Coroutines run **serially** |
| Sync API via `_run(coro)` from any thread | Multiple threads **block** waiting on one queue |
| `_get_option()` may fetch **full SPX chain** on cache miss | First spread close in a burst can delay others |
| `get_broker()` in `broker_factory.py` creates **new** broker per call | Each **process** may hold multiple sessions if callers don’t share (dashboard vs stop_monitor) |

### 3.5 AlertListener

- Separate asyncio loop in background thread.
- Registers **per stop order id** → `queue.Queue` consumed by owning `StopMonitor`.
- Round-robin design must **re-home** fill routing (central registry keyed by order id → trade id).

### 3.6 Limits and unused guards

- `MAX_BREACH_THREADS = 12` is **defined** in `monitor.py` but **not enforced** anywhere — concurrent breach workers can exceed 12 today.

**Decision — worker cap at 12 vs 40+ open trades (MEIC, Manual, METF, etc.):**

| Limit | Purpose | Paper / load test | First live rollout |
|-------|---------|-------------------|-------------------|
| `max_concurrent_exit_jobs` | Exit worker cap | `STOP_MAX_EXIT_JOBS` **16–24** | **8–12** — increase only after clean 429/error metrics |
| `max_broker_in_flight` | TT HTTP semaphore | **8** | **4–6** — increase only after clean metrics |
| `active_slots` (supervisor) | Round-robin scan set | **No cap** | **No cap** |

Formula: `max_concurrent_exit_jobs = min(open_slots, STOP_MAX_EXIT_JOBS)`.

**Important distinction:**

- **40 open trades** does **not** mean 40 exit workers at once. Normally only the supervisor scans all 40; workers spin up **only** when breach / stop fill / kill fires (typically a handful).
- **Worst case** (killswitch, market crash): many slots may enqueue jobs simultaneously — that is when `max_concurrent_exit_jobs` + broker semaphore matter. Queue excess jobs FIFO (manual kill priority per §6.3); do not drop them.
- **METF / future strategies:** `iter_active_trade_paths()` already walks all `trades/active/*` strategy dirs — V3 slot discovery stays strategy-agnostic; limits are **global per stop_monitor process**, not per strategy.

**Action:** Remove hardcoded `12`; document env/config in `STOP_MONITOR_V3` settings; enforce at `ExitWorkerPool.submit()`.

**Pool cap formula (open before multi-strategy at 40+ slots):** Do not lock a number until paper load test. Proposed shape:

```
max_concurrent_exit_jobs = min(open_slots, STOP_MAX_EXIT_JOBS)
```

where `STOP_MAX_EXIT_JOBS` is env-configured. **Paper:** 16–24; **first live:** 8–12. Exits are safety-critical — do not start live at paper-tuned ceilings until 429/backpressure logs are clean.

### 3.7 All active phase plugins (confirmed live)

All three phases in `blocks/stop/phases.py` are **live in production** via `meic_stop_profile()` in `blocks/stop/profiles/meic.py` (registered in `stop_profile.py`):

| Phase | Class | Trigger (`should_activate`) | Action (`execute`) |
|-------|-------|----------------------------|-------------------|
| 1 | `Phase1InitialStop` | `status == open` | Software breach detection + limit close |
| 2 | `Phase2NetCreditUpgrade` | `status == open` AND long MQTT ≤ **$0.05** AND stop not yet replaced | `upgrade_to_spread_stop()` — 2× net credit stop |
| 3 | `Phase3SpxProximityClose` | `status == open` AND CT ≥ **14:51** (`STRK_CHK_MIN`) | `execute_spx_proximity_close()` — market-close short if SPX within strike band |

**Not optional, not deprecated.** Manual trades using the MEIC credit-spread profile get the same three phases. V3 supervisor must invoke **all registered phases** from the trade’s `StopProfile` each scan cycle (`phase.should_activate()` → enqueue `ExitWorker` on `execute`), same as today’s phase loop in `_poll_once()` — not Phase 1 only.

Phase 2 and Phase 3 also use `_threaded_phase_execute` today; V3 routes them through `ExitWorkerPool` like Condition 1 breach work.

---

## 4. Target architecture (V3 overview)

```
┌─────────────────────────────────────────────────────────────────┐
│  StopSupervisor (single thread, round-robin)                     │
│  TARGET_CYCLE_SEC (~0.25s default), variable sleep after scan   │
│  for each open TradeSlot:                                        │
│    1. merge_disk_state(slot) — mtime cache; reload only on change (§8.1) │
│    2. drain AlertListener events for this trade's order ids      │
│    3. check command files (kill / stop_update)                   │
│    4. MQTT fast path: breach threshold (no broker)               │
│    5. if status==closing: schedule/poll (minimal broker)         │
│    6. slow path (every N cycles): REST stop reconcile            │
│                                                                  │
│  on condition A|B|C → submit ExitJob to WorkerPool (§6)           │
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  ExitWorkerPool (bounded concurrency, e.g. 4–12)               │
│  each job runs one ExitHandler pipeline to completion            │
│  broker calls go through BrokerLane (§7) — parallel ACROSS trades│
└───────────────────────────┬─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│  BrokerLane / TastyTradeBroker V2 (§7)                           │
│  ordered steps WITHIN a trade; concurrent requests ACROSS trades │
└─────────────────────────────────────────────────────────────────┘
```

**Principles:**

- **Fast path stays broker-free** except explicit slow-sync ticks and closing-state polls.
- **Spin out on condition** — supervisor never blocks on `_cancel_stop_and_confirm()` (up to 30s).
- **After manual kill detected** — slot enters **close-only mode** (no breach evaluation) until `closed` or failed/ retry policy exhausted.
- **JSON cache with mtime gate** — `slot.state` is the hot-path cache; `stat().st_mtime` decides when to re-read disk (§8.1). Live prices and breach math use **MQTT**, not JSON. Kill/stop commands use **separate command files** checked every cycle.

**Supervisor sleep policy:**

**Hybrid recommended — not fully zero sleep, not fixed 3s idle.**

| Approach | 40 slots, MQTT-only scan ~1ms | Risk |
|----------|-------------------------------|------|
| **No sleep (tight loop)** | CPU spins; breach detected in **microseconds** after MQTT move | High CPU (~one core busy); laptop fan; harder to reason about cadence |
| **Fixed `sleep(3)`** | Wastes time when scan takes 1ms | Up to 3s lag detecting breach/kill command — **bad** |
| **Variable sleep (GAP-22)** | `sleep(max(0, TARGET_CYCLE_SEC - elapsed))` with `TARGET_CYCLE_SEC ≈ 0.1–0.5s` | Bounded scan rate; sub-second breach pickup; low CPU |

**Proposal for V3:**

- Default **`TARGET_CYCLE_SEC = 0.25`** (4 full scans/sec over all slots) — much faster than today’s effective 3s **per-thread** stagger, without a busy-wait loop.
- **⚠ I/O caveat (must not lock default without this):** At 40 slots × 4 scans/sec, a naive `merge_disk_state()` that **re-reads JSON every cycle** ≈ **160 disk reads/sec**. MQTT breach math is ~1ms; **disk I/O can dominate** and invalidate the 0.25s default. **Required:** mtime-gated merge (§8.1) — only `load_state()` when `path.stat().st_mtime` changes or supervisor has dirty in-memory writes pending. Re-profile with 40 active files before sign-off; acceptable range remains **0.1–0.5s** once merge is cheap.
- **Optional tight mode** (`STOP_SUPERVISOR_SLEEP_MS=0`) for debugging or extreme volatility — operator-toggle, not default.
- **Yield point required:** even with no sleep, insert `time.sleep(0)` or await idle every N cycles so other threads (AlertListener, workers) get scheduler time — pure infinite loop can starve them on some platforms.

**Your instinct is right** that 3s sleep is too coarse for a single supervisor serving 40 legs; **dropping sleep entirely** trades CPU for marginal latency gain once cycle time is already ~1ms. Prefer **short variable sleep (~100–250ms)** as the production default **after** mtime-gated merge is implemented and profiled at target slot count.

---

## 5. Three exit conditions (detailed)

### 5.1 Condition 1 — Software breach (MQTT)

**Trigger (unchanged logic):**

- `status == open`
- Streamer not stale (same freeze rules as today)
- `spread_mark_price(short_mqtt, long_mqtt) >= spread_breach_threshold(state)`  
  (2× net credit + offset — see `stop_math.py` / `Phase1InitialStop`)

**Actions (sequenced within handler):**

1. Cancel exchange stop on short leg → `_cancel_stop_and_confirm()` (up to 30s poll)
2. If stop **filled** during cancel → treat as **Condition 2** (exchange stop filled path); do not also send breach limit
3. Else place **short-leg BTC limit** at MQTT-derived price (`replace_with_limit_close`)
4. On short fill → set `status: closing`, `short_closed_at`, `short_close_price`
5. Wait **`long_close_delay_sec` (30s)** — wall clock, not “within 30s”
6. **Long chase** — reprice STC limit on long leg until filled (`_chase_long_close` logic)

**Spin-out:** Entire handler runs in `ExitWorker`; supervisor marks slot `exit_in_progress` / `_breach_active` equivalent.

**Phase 2 and Phase 3 (confirmed live — §3.7):** Not part of the three *exit conditions* table, but **must run on the supervisor scan path** for `status == open` slots. Today’s `_poll_once()` iterates `profile.phases` in priority order; V3 preserves this:

```python
for phase in profile.phases:          # Phase1, Phase2, Phase3
    if phase.should_activate(slot):
        enqueue(ExitHandler.PHASE_EXECUTE, slot, phase=phase)
        break                         # same single-phase-per-cycle semantics as today
```

Phase 2 stop upgrade and Phase 3 proximity close spin out via `ExitWorkerPool` (replacing `_threaded_phase_execute`). `close_only_mode` skips the entire phase loop.

**Manual kill vs breach precedence — resolved:**

If a manual close command or killswitch is detected, the slot enters **`close_only_mode` immediately**. Breach checks, phase execution, and stop upgrades are **skipped** for that slot. The only exception: stop already **filled** during cancel → route to **Condition 2** (§6.5), not spread close. Manual kill wins over new breach logic; breach handler must abort if `close_mechanism` or `close_only_mode` is already set.

### 5.2 Condition 2 — Exchange stop filled

**Trigger:**

- WebSocket alert on stop `order_id` → fill event **OR**
- Slow REST sync detects stop `filled` (`_reconcile_active_stop_with_broker`)

**Actions:**

1. `handle_stop_order_update()` — record short BTC fill, `status: closing`, `short_closed_at`
2. Wait **30s** (`long_close_delay_sec`)
3. Long chase (background worker)

**Note:** Short leg already closed at broker — **must not** use spread-close order (Change 2 applies to operator kill only).

**Parallelism:** Independent trades can be in long-chase phase simultaneously; each issues separate STC orders — broker lane must allow concurrent **different** order ids.

### 5.3 Condition 3 — Manual close / kill

**Trigger:**

- Per-trade `trades/commands/{filename}.close.json` with `close_mechanism: manual_close`
- Global `trades/killswitch.json` → `admin_killswitch` on **all** open slots

**Actions (operator spec):**

1. **Stop watching** — persist `close_only_mode=true` + `exit_handler` to JSON (§8.5, §6.6) before worker runs; skip breach phases
2. Cancel exchange stop → confirm cancelled (same as today)
3. If stop filled during cancel → **route to Condition 2** (§6.5)
4. Else **one spread-close debit order** priced per **quote fallback order** below (not MQTT-only)
5. Poll until spread order filled / retry on reject
6. Persist **both leg fills**; apply **`long_close_price = 0.0` when missing** (operator rule Jul 2) until broker returns leg data
7. `move_to_closed`, unregister alerts, remove slot from supervisor active set

**Manual kill quote fallback (required):**

`ManualKillHandler` must **not** silently abort forever because MQTT mids are missing. Manual kill is an operator safety action.

Price source order:

1. **MQTT** market mids (primary — same as today `replace_with_spread_close`)
2. **Broker** quote/mid if available via REST
3. **Conservative fallback** limit using last known quote + configured emergency offset (env `MANUAL_KILL_EMERGENCY_OFFSET`, document default)
4. If no quote source exists: persist `exit_error=missing_quotes`, keep `close_only_mode=true`, log critical + dashboard alert — **do not** resume breach/phase scanning

**Spin-out:** Full pipeline in `ExitWorker` (`ManualKillHandler`).

**Dashboard:** `killSelected()` can remain sequential `fetch` (minor); optional later batch API.

---

## 6. Round-robin supervisor design

### 6.1 TradeSlot abstraction

Replace “one thread owns one JSON forever” with a **slot table**:

```python
@dataclass
class TradeSlot:
    path: str
    state: dict              # in-memory cache — authoritative for supervisor between merges
    disk_mtime: float        # last seen path.stat().st_mtime; skip load_state if unchanged
    _dirty: bool             # True after we save this slot; cleared on merge
    close_only_mode: bool
    exit_job_id: Optional[str]
    last_broker_sync: float
    long_chase_scheduled_at: Optional[float]
    alert_order_ids: set[str]
```

**Lifecycle:**

| status | Supervisor behavior |
|--------|---------------------|
| `open` | Full fast scan + breach |
| `closing` | Poll spread close OR timer for long chase; **no breach** |
| `closed` / `cancelled` | Remove slot; no worker |

### 6.2 Main loop (pseudocode)

```python
def supervisor_loop():
    while running:
        t0 = time.monotonic()
        slots = discover_open_trades()  # glob active dirs, same gates as runner.add()

        for slot in slots:
            merge_disk_state(slot)        # mtime-gated cache — §8.1; stat() only if skip
            drain_alert_queues(slot)
            if check_command_files(slot): # always — separate files, not trade JSON mtime
                enqueue(ExitHandler.MANUAL_KILL, slot)
                continue
            if slot.status == 'closing':
                handle_closing_poll(slot)  # may enqueue LONG_CHASE when timer due
                continue
            if slot.close_only_mode or slot.exit_job_id:
                continue                   # exit worker owns the trade
            if streamer_stale(slot):
                continue
            run_phase_scan(slot)           # Phase1 breach + Phase2/3 — §5.1, §3.7
                # enqueues ExitWorker when phase.should_activate → execute
            if slow_sync_due(slot):
                reconcile_stop(slot)       # may enqueue EXCHANGE_STOP_FILLED

        elapsed = time.monotonic() - t0
        sleep(max(0, TARGET_CYCLE_SEC - elapsed))  # default ~0.25s — see §4 sleep policy
```

This matches GAP-22: **variable wait** after servicing all legs (sub-second target, not 3s).

### 6.3 ExitWorkerPool

| Parameter | Proposed starting point | Notes |
|-----------|-------------------------|-------|
| `max_concurrent_exit_jobs` | **Formula** `min(open_slots, STOP_MAX_EXIT_JOBS)` | Paper: 16–24; **live rollout: 8–12** — §3.6 |
| Job identity | **One active exit owner per `path`** | See §6.3 idempotency — no duplicate handlers |
| Job queue | FIFO with **manual kill priority** | Operator kills jump ahead of breach |

**Worker responsibilities:**

- Run handler state machine to completion (or until `closing` + hand back poll responsibility).
- All broker calls via `BrokerLane` (§7).
- Save state to disk at defined checkpoints (reuse `state_mod.save_state`).
- On unhandled exception: log, set recoverable flag, supervisor may retry with backoff.

### 6.3.1 Exit idempotency (one active exit owner per trade)

**Rule:** One trade file may have **only one active exit owner** at a time (`exit_job_id` set OR worker holds trade lock).

If manual kill, software breach, REST stop fill, and WebSocket stop fill arrive in the **same cycle**, apply this precedence (higher wins; lower events are logged and ignored):

| Priority | State / event | Action |
|----------|---------------|--------|
| 1 | `status == closed` or `cancelled` | No-op; all exit events ignored |
| 2 | Stop **filled** (alert or REST) | **Condition 2** — do **not** place spread close or new breach limit |
| 3 | Manual kill / killswitch detected | **Condition 3** (or C2 if stop filled during cancel); skip breach + phases |
| 4 | Software breach (MQTT) | **Condition 1** only if slot not in `close_only_mode` and no exit job |
| 5 | Duplicate event (same condition, job already running) | Log `exit_duplicate_ignored`; no second worker |

**Implementation guards:**

- `ExitWorkerPool.submit(path, handler)` returns early if `path` already has active job.
- Handlers check `status in ('closing', 'closed')` at every broker step (port existing `handle_stop_order_update` guards).
- Alert + REST both reporting fill → second event is no-op.
- Killswitch + per-trade `.close.json` same cycle → first sets `close_only_mode`; second is no-op (§6.3.1).

### 6.3.2 Stuck exit job policy

Every `ExitJob` must update persisted heartbeat fields (§8.5):

- `exit_last_step` — current pipeline step name
- `exit_last_progress_at` — ISO timestamp of last forward progress
- `exit_attempt` — increment on retry

If no progress for **`STOP_EXIT_STALL_SEC`** (env, default **120s**), supervisor logs **critical** alert and sets `exit_stalled=true` on the trade JSON.

**Recovery rule:** A stalled exit job must **not** allow a second worker to double-close the trade unless recovery policy **proves** no live broker order exists (query broker for working spread close / stop / long STC before spawning replacement worker). Prefer operator alert + manual intervention over automatic double-submit.

### 6.4 StopMonitor migration

V3 targets **Option A only** — no Option B wrapper milestone.

Existing `StopMonitor` methods port into:

| Module | Responsibility |
|--------|----------------|
| `ManualKillHandler` | Condition 3 — cancel, spread close, quote fallback |
| `SoftwareBreachHandler` | Condition 1 — breach limit close |
| `ExchangeStopFilledHandler` | Condition 2 — 30s + long chase |
| Shared helpers | Stop cancel/confirm, spread-close poll, long chase, fill application, recovery |

`StopSupervisor` + `TradeSlot` replace `MonitorRunner`. **`MonitorRunner` / per-trade `StopMonitor` remain behind `STOP_MONITOR_ENGINE=v2` rollback flag** (§10) until V3 passes paper tests and at least one controlled live session. Do not delete V2 path until then.

### 6.5 AlertListener in round-robin

Today: each per-trade thread registers its stop order id.

V3:

- Central **`order_id → path`** map in supervisor.
- On stop replace/cancel: update registration (`_reregister_alert` logic moves to supervisor).
- On fill event: mark slot for **Condition 2** or inject into worker if already running.

**Kill vs stop-filled race — resolved:**

Sequence:

1. Operator writes kill command; worker calls `cancel_order` on exchange stop.
2. Market moves; broker reports stop **`filled`** (cancel not honored).
3. **Do not** place spread close — short leg is already gone.

**Behavior:**

- `_cancel_stop_and_confirm()` returns **`filled`** → call `handle_stop_order_update()` → **`close_mechanism`** remains `manual_close` (operator intent) but **pipeline = Condition 2**: `status: closing`, `short_closed_at`, wait **30s**, long chase.
- Idempotent guards prevent double-close if alert and REST both report fill.
- Explicit policy in `ManualKillHandler` (§5.3 step 3).

### 6.6 Command claiming

At ~0.25s scan cadence, command files need **exact ownership** — supervisor must not re-detect the same kill every cycle while the worker queue is saturated.

**Command claiming rule:**

When supervisor detects `{filename}.close.json`:

1. **Read** command payload.
2. **Claim atomically** — rename to `{filename}.close.processing.{pid}.{timestamp}.json` **or** delete only after `close_only_mode` + `exit_handler` + `exit_started_at` are **persisted** to trade JSON (§8.5).
3. **Persist** `close_only_mode=true`, `exit_handler=manual_close`, `exit_started_at` (atomic write — §8.3).
4. **Enqueue** `ManualKillHandler`.

If enqueue fails because trade already has an exit owner: log `manual_close_duplicate_ignored`, archive/remove command file, do not re-queue.

**`.stop_update.json`:** Use the same atomic claim/archive pattern; apply immediately or enqueue only if the update requires broker work (e.g. stop replacement on exchange).

**Killswitch:** Claim `killswitch.json` once globally, then fan out per-trade manual-close jobs (each trade gets its own claim + persist + enqueue per steps 2–4 above).

### 6.7 Recovery on stop_monitor restart

Today: `_on_load()` / `_recover_closing_on_load()` per thread.

V3: On startup, supervisor builds slots from all `active/*.json` with `open` or `closing`:

- If `close_only_mode` or `exit_handler` set → resume appropriate handler (do **not** resume breach/phases)
- If `spread_close_order_id` set → resume **Condition 3** poll
- If `short_closed_at` set and no spread close → resume **Condition 2** long-chase timer
- If `exit_stalled=true` → alert + broker reconcile before new worker (§6.3.2)

---

## 7. Broker parallelization design

### 7.1 Problem precisely stated

Parallel **Python threads** exist today, but **TastyTrade HTTP work is serialized** through one asyncio loop. Jul 2: four threads each blocked in `_cancel_stop_and_confirm`, executing broker ops **one after another**.

**Goal:** Trade **A**’s cancel+place can proceed concurrently with trade **B**’s cancel+place.

**Constraint:** Trade **A**’s steps **must remain ordered** (cancel before place; don’t poll fill before place returns).

### 7.2 BrokerLane API (conceptual)

```python
class BrokerLane:
    """Cross-trade concurrency, per-trade sequencing."""

    def run_trade_pipeline(self, trade_id: str, steps: Iterable[Callable]) -> None:
        """Acquire per-trade lock; run steps sequentially."""

    async def submit(self, coro_factory) -> Any:
        """Acquire global semaphore (N concurrent TT HTTP ops); run one coro."""
```

- **`trade_id`** = basename of JSON or `(lot, side)`.
- **`global_semaphore`** = tunable — **paper:** 8 (max 16–24 under load test); **first live:** 4–6. Increase only after 429/error metrics clean. Independent of slot count (40+).

**Rollout policy:** Exits are safety-critical. TastyTrade rate-limit behavior under parallel exit load is still unknown (§7.4). Start live conservatively; tune up from metrics, not down from failures.

### 7.3 Implementation options (must evaluate before coding)

| Option | Description | Pros | Cons / unknowns |
|--------|-------------|------|-----------------|
| **P1 — Concurrent coroutines, one loop** | Refactor `TastyTradeBroker._run` to schedule tasks; use `asyncio.Semaphore(N)` inside loop | Single session; true concurrent HTTP if httpx supports | SDK may not be thread-safe for shared session; need audit of `tastytrade` package |
| **P2 — Multiple brokers, one session** | Share `session` object across N loops | Theoretical parallelism | **Unknown if session is thread-safe**; may corrupt OAuth state |
| **P3 — Multiple brokers, N sessions** | One OAuth session per lane | Hard isolation | TT may rate-limit or reject; token refresh complexity |
| **P4 — Process pool** | Fork/spawn workers with own broker | Strong isolation | JSON state coordination hard; overkill |
| **P5 — Sync httpx pool in thread pool** | Bypass asyncio for order path | Simple mental model | Duplicates SDK usage; maintenance |

**Recommendation for spike:** **P1 first** — instrument concurrent `get_order_status` + `cancel_order` across four dummy order ids in paper; measure wall time vs serial. If session breaks, fall back to **P3 with N=2–4** not 12.

### 7.4 Rate limits (429)

Observed on **2026-07-01** entry: parallel PUT/CALL workers hit **429** on SPX quote fetch (`LIVE_SESSION_2026-07-01.md`).

**Implications:**

- Parallel stop exits may **increase** 429 frequency if semaphore too high.
- Need **retry with backoff** (partially exists in `_retry_on_transient`) and **global rate limiter** shared with entry monitor? Entry runs in **same launcher process** but different threads — **shared broker instance is NOT shared today** between entry workers and stop_monitor subprocess.

**Within stop_monitor subprocess only:** tune semaphore; log 429; adaptive backoff.

**Cross-process:** entry + stop_monitor each have own TT session — TT sees **two clients** — aggregate rate limit unknown. **Do not assume independence.**

### 7.5 Operations that must NOT be parallelized globally

| Operation | Reason |
|-----------|--------|
| Two handlers modifying **same** trade JSON | State corruption |
| `_get_option` chain rebuild storms | First call loads entire chain; need **mutex + warm cache** at session start or pre-market |
| `validate_session()` during active order burst | Could pause all lanes — schedule refresh in idle window |

### 7.6 Per-trade ordered pipelines (reference)

**Manual kill (Condition 3):**

```
cancel_stop → confirm → place_spread_close → [poll fills]* → finalize
```

**Software breach (Condition 1):**

```
cancel_stop → confirm → [if filled → C2] else place_short_limit → [poll]* → C2 long phase
```

**Exchange stop filled (Condition 2):**

```
record_short_fill → sleep(30) → long_chase_loop
```

Each pipeline holds **trade lock** for its duration; different trades run in parallel up to semaphore limit.

---

## 8. State, persistence, and races

### 8.1 Source of truth and JSON cache

- **Disk JSON** under `trades/active/` is authoritative **across processes** (dashboard, stop_monitor, entry).
- **In-memory `slot.state`** is the supervisor’s **working cache** for the fast scan path. Disk is re-read only when something external (or our own save) changes the file.

**Operator decision — cache + mtime (confirmed):**

Between entry and exit, trade JSON is **not** updated with live PnL or minute-by-minute marks. Those come from **MQTT**. During a steady `open` trade the file is often **unchanged for long stretches** — only event-driven writes (stop placed, phase upgrade, fill promotion, operator edit). Therefore:

| Layer | Updated every scan? | Source |
|-------|---------------------|--------|
| Breach / spread prices | **Yes** | MQTT streamer (not JSON) |
| Kill / stop× commands | **Yes** | `trades/commands/*.json` (separate files) |
| Trade state (order ids, status, stops) | **Only on mtime change** | Cached `slot.state` + `merge_disk_state()` |

**Steady-state model:** *read once into cache, merge when `mtime` changes.* This is what makes sub-second `TARGET_CYCLE_SEC` viable at 40+ slots without ~160 JSON parses/sec.

**mtime-gated merge (required):**

Today `_maybe_merge_disk_stop_state()` only reloads for stop-order drift; V3 centralizes a general merge policy but **must not** call `load_state()` every slot every cycle.

```python
def merge_disk_state(slot: TradeSlot) -> None:
    try:
        mtime = slot.path.stat().st_mtime
    except OSError:
        return
    if mtime <= slot.disk_mtime and not slot._dirty:
        return                          # cache hit — no JSON parse
    disk = state_mod.load_state(slot.path)
    slot.state = merge_policy(slot.state, disk)   # port _maybe_merge_disk_stop_state rules
    slot.disk_mtime = mtime
    slot._dirty = False

def save_slot(slot: TradeSlot) -> None:
    atomic_save_state(slot.path, slot.state)   # §8.3 — never partial overwrite
    slot.disk_mtime = slot.path.stat().st_mtime
    slot._dirty = False
```

- **`stat().st_mtime`** is cheap (microseconds per file) vs `load_state()` (read + parse).
- **`slot._dirty`:** set if we need to force a re-merge before mtime visible (rare); cleared after save or merge.
- **Worst case without mtime gate:** 40 slots × 4 cycles/sec ≈ **160 JSON reads/sec** — I/O-bound before MQTT matters.
- **Profile before locking `TARGET_CYCLE_SEC`:** measure scan ms with 40 files, cache hot vs cold.

**What still runs every cycle (no trade JSON read):**

1. `path.stat().st_mtime` per slot (or batch stat)
2. `check_command_files()` — `.close.json`, `.stop_update.json`, `killswitch.json` (these do **not** bump trade JSON mtime)
3. MQTT price lookup + breach / phase `should_activate()`
4. Alert queue drain

**When trade JSON mtime changes — cache invalidates, supervisor re-reads:**

| Writer | Typical event |
|--------|----------------|
| Entry monitor | `pending_fill` → `open`, leg fill prices |
| `pending_fill_sync` | Open-order fill promotion (launcher / dashboard / runner) |
| Dashboard | Stop× edit (`/api/stop_multiplier` writes JSON + command file) |
| stop_monitor (worker) | Stop placed, phase 2 upgrade, broker reconcile save, exit fields |
| **Operator manual edit** | Fix bad JSON on disk → save → **mtime bumps → next scan reloads cache** |

**Manual JSON edit (operator workflow):**

If a trade JSON has an error, editing and saving the file in `trades/active/` updates `mtime`. On the next supervisor pass, `merge_disk_state()` sees `mtime > disk_mtime`, re-reads, and merges into `slot.state`. No restart required. This is an intentional benefit of mtime-based invalidation — same mechanism as entry or dashboard writes.

**Not written every scan today (V3 preserves):**

- `breach_watch` snapshot is built **in memory** each poll (`_refresh_breach_watch`) but **not** saved to disk every cycle — only on event saves (e.g. streamer stale). Breach **detection** uses live MQTT, so cache does not weaken safety. Dashboard may show slightly stale `breach_watch` on disk until next save (same as today).

**Existing merge helper:** `_maybe_merge_disk_stop_state()` in `monitor.py` — merge rules port into `merge_policy()`.

### 8.2 Writers (what bumps mtime vs what does not)

| Writer | Touches trade JSON? | Touches command files? | When (during `open`) |
|--------|---------------------|------------------------|----------------------|
| Supervisor fast path | Optional (`breach_watch` on events only) | No | Rare — not every scan |
| ExitWorker | **Yes** | No | Stop place, phase upgrade, exit pipeline |
| Entry monitor | **Yes** | No | Fill promotion, partial fills |
| `pending_fill_sync` | **Yes** | No | Open-order sync (separate process timing) |
| Dashboard kill | No | **Yes** (`.close.json`) | Operator kill |
| Dashboard stop× | **Yes** + `.stop_update.json` | **Yes** | Operator multiplier change |
| Operator manual edit | **Yes** | No | Fix JSON on disk → mtime → cache reload |

**Implication:** Command-driven actions (kill) are visible **every scan** via command-file checks. Trade JSON cache does **not** delay kill detection.

### 8.3 Atomic state writes

**Required:** All trade JSON writes (supervisor, workers, entry, dashboard) must be **atomic** — V3 increases concurrent writers on the same file lifecycle.

```python
def atomic_save_state(path: str, state: dict) -> None:
    dir_name = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())          # best effort on Windows
        os.replace(tmp, path)             # atomic on same filesystem
    except Exception:
        os.unlink(tmp)
        raise
```

- Write to **temp file in same directory** → flush → `os.replace(temp, target)`.
- **Never** partially overwrite active trade JSON in place.
- After replace, update slot `disk_mtime` (supervisor cache stays coherent).
- Port into `blocks/stop/state.py` `save_state()` — used by V2 and V3.

### 8.4 File locking and cross-process races

**Today:** No explicit lock; relies on single writer per file per process (one thread per trade). V3 **multiple workers** can touch **different** files safely; same file protected by **one job per path** rule (§6.3.1).

**Cross-process:** Entry could write same file while stop_monitor exits — mitigated by status gates (`open` → `closing` → `closed`) and atomic writes.

### 8.5 Required V3 JSON fields

In-memory `close_only_mode` alone is **not enough** — if stop_monitor restarts after manual kill is accepted but before close completes, V3 must **not** resume breach/phase scanning.

| Field | Required? | Purpose |
|-------|-----------|---------|
| `close_only_mode` | **Yes** | Survives restart after manual kill accepted; skip breach/phases |
| `exit_handler` | **Yes** | `manual_close` / `stop_filled` / `breach` / `phase2` / `phase3` — recovery routing |
| `exit_started_at` | **Yes** | Timeout/retry metrics; stall detection |
| `exit_last_step` | Recommended | Recovery checkpoint; stuck-job diagnostics |
| `exit_last_progress_at` | Recommended | `STOP_EXIT_STALL_SEC` watchdog (§6.3.2) |
| `exit_attempt` | Recommended | Backoff and duplicate prevention |
| `exit_stalled` | Recommended | Set when stall threshold exceeded |
| `exit_error` | Optional | e.g. `missing_quotes` — operator alert (§5.3) |

Set on command claim (§6.6) **before** worker enqueue. V2 trades without these fields: infer from `close_mechanism` + `status` + `spread_close_order_id` on first V3 load, then persist.

### 8.6 Fill recording (operator rule)

When `long_close_price` is **null** on closed manual/spread-close trades:

- **Display / PnL default:** treat as **`0.0`**, not open long fill.
- **Still fix broker parsing** to store actual STC when available (`monitor._apply_spread_close_fill()` / `OrderResult.long_fill_price` from `tastytrade_broker.py`).

**Exact code changes (`blocks/stop/close_fills.py`):**

Today `_resolved_long_close_price()` **infers** from open `long_leg.fill_price` when `long_close_price` is null but `short_close_price` is set — this misstates spread-close PnL (Jul 2: showed credit exit when long STC was unknown):

```46:56:blocks/stop/close_fills.py
def _resolved_long_close_price(state: Dict[str, Any]) -> Optional[float]:
    """Long STC fill; when missing, infer from open long fill (spread-close JSON gap)."""
    long_close = state.get('long_close_price')
    if long_close is not None:
        return float(long_close)
    if state.get('short_close_price') is None:
        return None
    long_fill = (state.get('long_leg') or {}).get('fill_price')
    if long_fill is not None and float(long_fill) > 0:
        return float(long_fill)
    return None
```

**Change (V2.9 — ship immediately; does not wait for V3 supervisor):**

1. **Remove lines 53–55** — delete the `long_leg.fill_price` inference branch entirely.
2. When `short_close_price` is set and `long_close_price` is null, return **`0.0`** (not `None`) for spread-close / manual-close mechanisms only:

```python
def _resolved_long_close_price(state: Dict[str, Any]) -> Optional[float]:
    long_close = state.get('long_close_price')
    if long_close is not None:
        return float(long_close)
    if state.get('short_close_price') is None:
        return None
    mechanism = str(state.get('close_mechanism') or '').lower()
    if mechanism in ('manual_close', 'admin_killswitch'):
        return 0.0
    return None   # stop/breach paths: no inference — slippage stays None until both legs known
```

3. **`brokerage_spread_exit_debit()`** — will compute `short − 0.0` for manual kills with missing long; operator slippage paths unaffected (`_qualifies_for_operator_slippage` excludes manual).
4. **Tests to update:** `tests/test_close_fills.py` — assert null long + manual_close → `0.0`, not open-fill inference; stop-out with null long still returns `None` for slippage.
5. **Broker-side (separate):** improve `_apply_spread_close_fill()` / TT order status parsing so `long_close_price` is persisted when the API returns per-leg fills — operator `0.0` is the **display fallback**, not a substitute for real STC data.

---

## 9. Touch points outside stop_monitor

| Component | Change needed |
|-----------|---------------|
| `blocks/stop/close_fills.py` | **V2.9:** null-long → `0.0` policy (§8.6) — ship before V3 |
| `blocks/stop/state.py` | **V3:** atomic `save_state()` (§8.3) |
| `dashboard/templates/index.html` | Optional: parallel `fetch` for kill — low priority |
| `dashboard/server.py` | No change for V3 core |
| `blocks/entry/*` | No change; ensure new open trades register with supervisor |
| `run.py` / `blocks/stop/run.py` | **`STOP_MONITOR_ENGINE`** feature flag (§10); spawn v2 or v3 |
| `tests/test_close_fills.py` | **V2.9:** null-long policy; extend for V3 supervisor |
| `tests/test_spread_kill.py` | V3 handler tests (fake broker); round-robin supervisor |
| `meic0dte/logs/stop_monitor.log` | Already configured in `blocks/stop/run.py` — use for V3 metrics |

---

## 10. Rollback / feature flag

V3 must be launchable behind a feature flag with operational fallback to V2.

| Env | Engine | Behavior |
|-----|--------|----------|
| `STOP_MONITOR_ENGINE=v2` | Current | `MonitorRunner` + per-trade `StopMonitor` (default for first merge) |
| `STOP_MONITOR_ENGINE=v3` | New | `StopSupervisor` + `ExitHandler` modules |

**Rollout:**

- **Default for first merge:** `v2`
- Paper/live operator must **explicitly enable** `v3`
- Do **not** delete `MonitorRunner` / V2 path until V3 completes paper tests (§12.5) and at least **one controlled live session**

**Rollback requirement:**

If V3 startup fails, broker concurrency fails, or supervisor heartbeat is stale (`trades/heartbeat.json`), launcher must be able to:

1. Stop V3 stop_monitor subprocess
2. Restart with `STOP_MONITOR_ENGINE=v2`
3. **Without modifying** trade JSON files (V2 reads same `active/*.json`)

V2 must tolerate new V3 fields (`close_only_mode`, `exit_handler`, etc.) — ignore unknown keys or honor `close_only_mode` if present.

**Rollback test (required before live V3):** Test rollback while `close_only_mode=true` and `exit_handler=manual_close` are already persisted — restart with `STOP_MONITOR_ENGINE=v2` and verify V2 honors close-only state and does not resume breach/phase scanning on a half-started manual kill.

---

## 11. Migration phasing

**Strategy:** Option A supervisor remains the long-term target. **Do not** reintroduce Option B wrapper. **Do** ship one small pre-V3 patch for immediate exit correctness — orthogonal to `BrokerLane` and supervisor rewrite.

**Dropped as long-term paths:** Option B wrapper; incremental V3a supervisor bypass.

**V2.9 rationale:** Jul 2 showed misleading PnL from open-fill inference and blocking manual kills. `close_fills.py` is low risk and does not need `BrokerLane`, paper spike, or supervisor rewrite. Optional manual-kill threading fixes live *feel* without polluting V3 architecture.

### 11.1 Recommended implementation sequence

**Do not** build BrokerLane + full supervisor + all three handlers in one step. **Start with manual kill** (live pain point), then port other conditions.

```
Step 1 — V2.9 close_fills.py null-long fix + tests

Step 2 — Feature flag / rollback shell (§10)
         STOP_MONITOR_ENGINE=v2 default; v3 entry point stub

Step 3 — Fake-broker unit tests for V3 handlers (before live broker)

Step 4 — BrokerLane paper spike (V3-0)

Step 5 — TradeSlot + mtime cache + atomic save_state (§8.1, §8.3)

Step 6 — ExitWorkerPool + idempotent command claiming (§6.6, §6.3.1)

Step 7 — ManualKillHandler first (Condition 3)
         Paper: 4 simultaneous kills, kill→C2 race, missing-quote fallback

Step 8 — ExchangeStopFilledHandler second (Condition 2)

Step 9 — SoftwareBreachHandler + Phase 2/3 execution third (Condition 1)

Step 10 — Recovery + heartbeat + stuck-job policy + forced paper scenarios

Step 11 — First live V3 session (conservative caps §3.6); v2 rollback on standby
```

### 11.2 Phase table

> **Implementation note (2026-07-05):** All phases through V3-4 are shipped locally. See **§16.1** for status. Live C1/C2 validation is the only remaining rollout gate.

| Phase | Deliverable | Risk | When |
|-------|-------------|------|------|
| **V2.9** | **Urgent exit correctness patch** — `close_fills.py` null-long → `0.0` (§8.6); tests for `manual_close` / `admin_killswitch` | Low | **Shipped** |
| **V3-0** | Paper spike: broker parallelism P1 vs P3 (§7.3) | Low | **Done** |
| **V3-1** | `BrokerLane` + configurable limits; paper caps (§3.6) | Medium | **Shipped** |
| **V3-2a** | `StopSupervisor` + `ManualKillHandler` only (behind `v3` flag) | Medium | **Shipped** — live C3 Jul 4 |
| **V3-2b** | `ExchangeStopFilledHandler` + `SoftwareBreachHandler` + phases | High | **Shipped** — live C1/C2 pending |
| **V3-3** | Idempotency hardening, command claiming, stuck-job policy | Medium | **Shipped** |
| **V3-4** | Broker leg-parse fix; observability; **live rollout caps** | Low–Medium | **Shipped** |

---

## 12. Challenges requiring discussion

### 12.1 Architecture

1. **Round-robin vs hybrid** — Keep per-trade threads for `closing` state only, round-robin for `open`? Adds complexity; pure model preferred?
2. **Phase 2/3 plugins** — **Resolved (live):** all three phases registered in `meic_stop_profile()` (§3.7). Supervisor calls `phase.should_activate()` / enqueues `ExitWorker` for each; not optional.
3. **Manual kill vs stop-filled during cancel** — **Resolved:** stop filled during kill cancel → **Condition 2** (30s + long chase); no spread close (§6.5).
4. **Killswitch + individual kill** — **Resolved:** first detect sets `close_only_mode` + enqueues manual kill; second detect same cycle is no-op (§6.3.1).
5. **Exit idempotency under concurrent triggers** — **Resolved:** precedence table §6.3.1; one active exit owner per path.

### 12.2 Broker / TastyTrade

6. **Session thread-safety** — Can one `Session` drive concurrent httpx requests? **Spike required**; do not assume.
7. **Max concurrent orders** — TT account-level limits for SPX 0DTE bursts?
8. **Shared session across processes** — Should entry and stop_monitor share OAuth refresh? (GAP-07 partially addressed per-broker refresh thread.)

### 12.3 Correctness

9. **Long chase after spread close** — Jun 26 ms-50 bug if `short_closed_at` unset while `spread_close_order_id` working — guards must port to V3.
10. **0DTE freeze** — `_broker_actions_frozen()` after market close — supervisor must respect for all handlers.
11. **Partial fills** — `stop_qty_for_state` / partial stop resize — round-robin must not skip.

### 12.4 Observability

12. **Prove parallelism** — Structured logs: `{trade, handler, step, wait_ms, queue_depth}`.
13. **Heartbeat** — `trades/heartbeat.json` today counts threads; V3 should report `{active_slots, active_exit_jobs, broker_in_flight}`.

### 12.5 Testing

14. **Paper test:** 4 simultaneous manual kills — wall time < Jul 2 (~120s)? Target TBD.
15. **Simultaneous breach** on 6+ spreads — verify semaphore + thread cap.
16. **Restart mid-close** — each condition recoverable.
17. **Manual JSON edit** — operator saves fix to `active/*.json`; supervisor picks up within one `TARGET_CYCLE_SEC` without restart.
18. **Concurrent exit triggers** — manual kill + breach + stop fill same cycle; verify §6.3.1 precedence, no double-close.
19. **Stop filled during manual-kill cancel** — routes to C2 only; no spread close.
20. **V2 rollback mid manual-kill** — `close_only_mode=true` + `exit_handler=manual_close` persisted; restart with `STOP_MONITOR_ENGINE=v2` (§10).

## 13. Success metrics

### 13.1 Primary (broker-controllable milestones)

These are the **pass/fail** criteria for V3 rollout. “All trades closed” depends on market liquidity — keep as observed metric only (§13.2).

| Metric | Baseline (Jul 2) | Target |
|--------|------------------|--------|
| Kill command detected → `close_only_mode` persisted | N/A (inline) | ≤ **1 supervisor cycle** (~0.25s) |
| Kill command detected → exit job accepted | N/A | ≤ **1 supervisor cycle** |
| 4 manual kills → all **cancel requests submitted** | ~3s cancels | ≤ **3–5s** (broker semaphore bound) |
| 4 manual kills → all **spread-close orders submitted** (after cancel confirmed) | ~52s first wave | Measurable vs V2 baseline; primary parallelism win |
| Duplicate exit triggers → double-close attempts | unknown | **0** |
| Supervisor scan cost | N × sleep(3) threads | One thread; mtime-gated merge; profile at 40 slots |
| Concurrent broker HTTP ops | 1 effective | Paper: ≥ 8; **live start: 4–6**, tune up |
| Correct exit leg recording | long null / wrong inference | **V2.9:** null→0 on manual kill |

### 13.2 Observed (not primary gate)

| Metric | Baseline (Jul 2) | Notes |
|--------|------------------|-------|
| Kill command → all trades `closed` | ~123s (4 trades) | Fill-quality dependent; log for comparison |
| Time to full flat after killswitch | TBD | Market + broker dependent |

---

## 14. Reference — key files

### 14.1 V2 (rollback path)

| File | Role |
|------|------|
| `blocks/stop/state.py` | Atomic `save_state()` (§8.3); trade path iteration |
| `blocks/stop/runner.py` | V2 per-trade thread supervisor (rollback path) |
| `blocks/stop/monitor.py` | Poll loop, three exit paths, handlers |
| `blocks/stop/phases.py` | Phase 1/2/3 plugins — all live |
| `blocks/stop/profiles/meic.py` | MEIC stop profile — registers all three phases |
| `blocks/stop/stop_profile.py` | `StopProfile` dataclass + profile registry |
| `blocks/stop/alerts.py` | TT fill websocket |
| `blocks/stop/run.py` | CLI entry, `STOP_MONITOR_ENGINE` flag, logging path |
| `brokers/tastytrade_broker.py` | Asyncio broker wrapper; spread-close leg parse (V3-4) |
| `blocks/stop/close_fills.py` | Slippage + `_resolved_long_close_price` (**V2.9** null→0 fix) |
| `tests/test_close_fills.py` | V2.9 tests for manual_close null-long policy |
| `blocks/stop/breach.py` | `spread_mark_price`, breach threshold helpers |
| `dashboard/server.py` | `/api/close_trade`, `/api/killswitch` |
| `changes/GAP_ANALYSIS.md` | GAP-22 round-robin discussion |

### 14.2 V3 (implemented)

| File | Role |
|------|------|
| `blocks/stop/v3/supervisor.py` | `StopSupervisor` — single scan loop, handler dispatch, heartbeat |
| `blocks/stop/v3/trade_slot.py` | `TradeSlot` cache + mtime-gated `merge_disk_state()` |
| `blocks/stop/v3/broker_lane.py` | Per-trade lock + global semaphore (`in_flight` metric) |
| `blocks/stop/v3/exit_pool.py` | One exit job per trade path; manual-kill priority |
| `blocks/stop/v3/command_claim.py` | Atomic `.close.json` / killswitch claiming |
| `blocks/stop/v3/recovery.py` | V3 field backfill, stall detection, startup routes, broker reconcile |
| `blocks/stop/v3/quotes.py` | Manual kill pricing: MQTT → REST → emergency offset |
| `blocks/stop/v3/observability.py` | Structured `v3_exit` JSON log lines |
| `blocks/stop/v3/config.py` | Env tunables (`TARGET_CYCLE_SEC`, lane size, stall sec) |
| `blocks/stop/v3/handlers/manual_kill.py` | **Condition 3** — cancel stop, spread close |
| `blocks/stop/v3/handlers/exchange_stop_filled.py` | **Condition 2** — record short fill, schedule long chase |
| `blocks/stop/v3/handlers/software_breach.py` | **Condition 1** — phase.execute via exit pool |
| `blocks/stop/v3/handlers/long_chase.py` | Long-leg STC chase after stop fill |
| `blocks/stop/v3/handlers/monitor_adapter.py` | Shared `StopMonitor` adapter for handlers |
| `scripts/v3_broker_spike.py` | V3-0 read-only TT parallelism probe |
| `scripts/seed_dual_manual_kill_fixture.py` | Offline dual-kill fixture (sandbox or `--apply`) |
| `changes/STOP_MONITOR_V3_REVIEW_FIXES.md` | Post-ship review findings + fix tracker |

### 14.3 V3 tests (253 total in suite)

| File | Covers |
|------|--------|
| `tests/test_v3_manual_kill.py` | Quote fallback, ManualKillHandler, command claim |
| `tests/test_v3_exchange_stop.py` | C2 handler, long chase |
| `tests/test_v3_software_breach.py` | C1 phase execution |
| `tests/test_v3_recovery.py` | Stall detection |
| `tests/test_v3_paper_scenarios.py` | §12.5 paper: rollback, restart, idempotency, dual kill |
| `tests/test_v3_remaining.py` | Startup routes, stall reconcile, broker lane metrics |
| `tests/test_stop_monitor_engine.py` | `STOP_MONITOR_ENGINE` flag |
| `tests/test_dual_manual_kill_simulation.py` | V2-style dual kill baseline (mock) |
| `tests/test_tastytrade_leg_actions.py` | Spread-close BTC/STC leg fill extraction |

---

## 15. Summary

V3 converges on: **StopSupervisor** + **mtime-gated JSON cache** + **atomic writes** + **three exit handlers** (Option A) + **exit idempotency** + **command claiming** + **V2/V3 feature flag**. Kill-during-stop-fill → Condition 2. Manual kill wins over breach. **`close_only_mode` persisted to disk** (required field).

**Ship order (completed):** V2.9 → feature flag → V3-0 spike → V3-1 BrokerLane → V3-2a ManualKill → V3-2b C1/C2 handlers → V3-3 idempotency/stall/recovery → V3-4 observability/leg parse → paper scenarios.

**Remaining gate:** Live market validation of **Condition 1** (software breach) and **Condition 2** (exchange stop fill) on first V3 session with market open. Condition 3 (manual kill) live-validated 2026-07-04.

See **§16** for operator commands, env vars, and operational notes.

---

## 16. Implementation status and operator notes

*Added 2026-07-05 after offline implementation and paper test pass.*

### 16.1 Phase completion

| Phase | Deliverable | Status |
|-------|-------------|--------|
| **V2.9** | `close_fills.py` null-long → `0.0` | **Shipped** |
| **V3-0** | Broker parallelism spike (`scripts/v3_broker_spike.py`) | **Done** — ~2.7× speedup vs serial at lane=6 (Jul 4 after-hours probe) |
| **V3-1** | `BrokerLane` + env caps | **Shipped** |
| **V3-2a** | `StopSupervisor` + `ManualKillHandler` | **Shipped** — live dual-kill tested Jul 4 |
| **V3-2b** | `ExchangeStopFilledHandler` + `SoftwareBreachHandler` + `LongChaseHandler` | **Shipped** — paper/fake-broker only |
| **V3-3** | Command claiming, exit idempotency, stall policy, startup recovery | **Shipped** |
| **V3-4** | Observability, heartbeat, spread-close leg parse | **Shipped** |
| **Live C1/C2** | Breach + exchange stop fill on real positions | **Pending** — requires market hours |
| **V2 rollback** | `MonitorRunner` path retained | **Kept** — do not delete until post-live sign-off |

### 16.2 Enabling V3

Add to `.env` (do **not** commit — contains TT credentials):

```
STOP_MONITOR_ENGINE=v3
```

Rollback: set `STOP_MONITOR_ENGINE=v2` or unset (defaults to `v2`), restart stop_monitor. V2 honors persisted `close_only_mode` / `exit_handler` on trade JSON.

Optional tunables (defaults in `blocks/stop/v3/config.py`):

| Env | Default | Purpose |
|-----|---------|---------|
| `TARGET_CYCLE_SEC` | `0.25` | Supervisor scan interval |
| `STOP_BROKER_LANE_SIZE` | `6` | Max concurrent TT HTTP ops across trades |
| `STOP_MAX_EXIT_JOBS` | `12` | Max parallel exit worker threads |
| `STOP_EXIT_STALL_SEC` | `120` | No progress → `exit_stalled=true` + critical log |
| `MANUAL_KILL_EMERGENCY_OFFSET` | `0.50` | Debit cap when MQTT + REST quotes missing |

### 16.3 Useful commands

**Start stop monitor (from repo root):**

```powershell
cd MEIC-with-Dash-main-V2
python -m blocks.stop.run
# or with explicit poll (V3 ignores per-trade poll; uses TARGET_CYCLE_SEC):
python -m blocks.stop.run --poll 5
```

**Run V3 test suite:**

```powershell
python -m pytest tests/test_v3_manual_kill.py tests/test_v3_exchange_stop.py `
  tests/test_v3_software_breach.py tests/test_v3_recovery.py `
  tests/test_v3_paper_scenarios.py tests/test_v3_remaining.py -v

python -m pytest tests/ -q   # full suite (253 tests)
```

**Broker parallelism spike (read-only — no orders placed):**

```powershell
python scripts/v3_broker_spike.py --mock-only
python scripts/v3_broker_spike.py --order-ids 480934535 480934537
```

**Seed offline dual-kill fixtures (safe sandbox — stop_monitor does not watch):**

```powershell
python scripts/seed_dual_manual_kill_fixture.py
```

**Seed into active dir (stop_monitor WILL manage — use only for intentional live tests):**

```powershell
python scripts/seed_dual_manual_kill_fixture.py --apply --write-kill-commands
```

### 16.4 Logs and heartbeat

| Artifact | Location | Contents |
|----------|----------|----------|
| Stop monitor log | `meic0dte/logs/stop_monitor.log` | All supervisor/handler activity |
| Structured exit events | Same log, prefix `v3_exit` | JSON: `{trade, handler, step, wait_ms, queue_depth}` |
| Heartbeat | `trades/heartbeat.json` | `engine`, `loop_count`, `active_slots`, `active_exit_jobs`, `broker_in_flight`, `broker_lane_max`, `target_cycle_sec` |

**Example log grep (PowerShell):**

```powershell
Select-String -Path meic0dte/logs/stop_monitor.log -Pattern "v3_exit|Exit job started|Claimed manual close"
Get-Content trades/heartbeat.json | ConvertFrom-Json
```

### 16.5 Live validation summary (Jul 4, 2026)

| Path | Result |
|------|--------|
| **C3 Manual kill** | **Validated** — dual CCS kill: stops cancelled, spread closes placed (~3s), `close_only_mode` + `exit_handler` persisted. Requires **only one** stop_monitor process. |
| **C2 Exchange stop fill** | Code + paper tests pass; **not live-tested** (market closed / positions already in manual-kill close path) |
| **C1 Software breach** | Code + paper tests pass; **not live-tested** (requires open position + breach during market hours) |
| **Quote fallback** | Validated — `broker_rest` mids when MQTT absent after hours |
| **V3-0 spike** | Validated — parallel `get_order_status` ~2.7× vs serial, no 429 observed at lane=6 |

### 16.6 Hybrid architecture note (intentional)

V3 does **not** fully replace `StopMonitor` yet. The supervisor delegates these to existing monitor methods:

- Breach watch refresh (`_refresh_breach_watch`) — MQTT every cycle
- Stop placement / resize (`_ensure_stop_for_filled_qty`)
- Spread-close poll (`_poll_spread_close`) on `closing` trades with `spread_close_order_id`
- 0DTE freeze (`_broker_actions_frozen`)

**Exit execution** (cancel, place close, phase.execute, long chase) runs through V3 **ExitWorkerPool + BrokerLane + handlers**. This is the intended V3-2b hybrid; full monitor excision is a future cleanup, not a rollout blocker.

### 16.7 Operational warnings

1. **One stop_monitor at a time** — a lingering V2 instance can steal kill commands from V3.
2. **Do not re-kill the same spreads** to test C2 — manual kill sets `close_only_mode`; use fresh open test spreads for breach/stop-fill proof.
3. **Stall policy** — `exit_stalled=true` triggers broker reconcile + critical log; does **not** auto-spawn a second exit worker (operator review).
4. **Restart mid-close** — supervisor resumes spread-close poll or long chase from persisted JSON fields; no restart required for operator JSON edits (mtime merge).
5. **`.env` is gitignored** — set `STOP_MONITOR_ENGINE=v3` locally only.
6. **Restart during manual kill (before `closing`)** — fixed 2026-07-05 (F-1); supervisor re-enqueues `ManualKillHandler`. See [STOP_MONITOR_V3_REVIEW_FIXES.md](STOP_MONITOR_V3_REVIEW_FIXES.md).

### 16.8 Monday checklist (first live V3 session)

1. Confirm no stale stop_monitor processes running.
2. Start stop_monitor with `STOP_MONITOR_ENGINE=v3`.
3. Let any `closing` trades from prior tests finish (spread poll / long chase).
4. Monitor `heartbeat.json` and `v3_exit` log lines during session.
5. C1/C2 will validate naturally when breach or exchange stop fill occurs — no forced test required on production CCS.
6. Keep `STOP_MONITOR_ENGINE=v2` rollback ready if heartbeat goes stale or unexpected double-close attempts appear.
