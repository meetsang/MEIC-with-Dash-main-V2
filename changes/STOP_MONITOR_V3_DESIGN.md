# Stop Monitor V3 — Design Document

**Status:** Design only — no implementation in this pass  
**Date:** 2026-07-03  
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
| R4 | **Fill recording gap** on spread close (`long_close_price: null`) | Trade JSON + `_order_result_from_placed_order()` |

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

### 3.6 Unused / incomplete guards

- `MAX_BREACH_THREADS = 12` is **defined** in `monitor.py` but **not enforced** anywhere — concurrent breach workers can exceed 12 today.
~~~ No use of this. What if I am running multiple strategies like MEIC, Manual, METF etc and have 40 trades open?
---

## 4. Target architecture (V3 overview)

```
┌─────────────────────────────────────────────────────────────────┐
│  StopSupervisor (single thread, round-robin)                     │
│  every FAST_INTERVAL (~3s), variable sleep to maintain cadence  │
│  ~~~this might not need to sleep just let it go in infinite loop no sleep, what you think?                                                                │
│  for each open TradeSlot:                                        │
│    1. reload/merge state from disk (see §8)                      │
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

**Open questions:**

- If breach fires while **manual kill** command already written for same file — **precedence?** Proposed: **manual kill wins** (operator intent); breach handler aborts if `close_mechanism` already set.
- Phase 2/3 upgrades (stop replacement, proximity) — V3 doc assumes **Phase1 path only** unless we explicitly port phase plugins into round-robin. **Needs audit** of `phases.py` beyond `Phase1InitialStop`.

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

1. **Stop watching** — slot flags `close_only_mode`; skip breach phases
2. Cancel exchange stop → confirm cancelled (same as today)
3. If stop filled during cancel → **branch to Condition 2** (long chase only) OR **debate:** still attempt spread close if both legs open at broker? **Needs decision** — today `replace_with_spread_close` returns early on stop filled via `handle_stop_order_update`.
4. Else **one debit spread close** priced from MQTT mids + tick adjust (`replace_with_spread_close`)
5. Poll until spread order filled / retry on reject
6. Persist **both leg fills**; apply **`long_close_price = 0.0` when missing** (operator rule Jul 2) until broker returns leg data
7. `move_to_closed`, unregister alerts, remove slot from supervisor active set

**Spin-out:** Full pipeline in `ExitWorker` (`_threaded_spread_close` or unified handler).

**Dashboard:** `killSelected()` can remain sequential `fetch` (minor); optional later batch API.

---

## 6. Round-robin supervisor design

### 6.1 TradeSlot abstraction

Replace “one thread owns one JSON forever” with a **slot table**:

```python
@dataclass
class TradeSlot:
    path: str
    state: dict              # in-memory; merged with disk each cycle or on events
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
            merge_disk_state(slot)        # see §8
            drain_alert_queues(slot)
            if check_command_files(slot):
                enqueue(ExitHandler.MANUAL_KILL, slot)
                continue
            if slot.status == 'closing':
                handle_closing_poll(slot)  # may enqueue LONG_CHASE when timer due
                continue
            if slot.close_only_mode or slot.exit_job_id:
                continue                   # exit worker owns the trade
            if streamer_stale(slot):
                continue
            if breach_detected(slot):
                enqueue(ExitHandler.SOFTWARE_BREACH, slot)
                continue
            if slow_sync_due(slot):
                reconcile_stop(slot)       # may enqueue EXCHANGE_STOP_FILLED

        elapsed = time.monotonic() - t0
        sleep(max(0, FAST_INTERVAL - elapsed))
```
~~~i m just thinking if we need to drop sleep altogether.
This matches GAP-22 operator proposal: **variable wait** after servicing all legs.

### 6.3 ExitWorkerPool

| Parameter | Proposed starting point | Notes |
|-----------|-------------------------|-------|
| `max_concurrent_exit_jobs` | 12 (`MAX_BREACH_THREADS`) | Enforce constant that exists but is unused today |
| Job identity | one active job per `path` | Prevent duplicate handlers on same trade |
| Job queue | FIFO with optional **manual kill priority** | Operator kills jump ahead of breach? **Discuss** |

**Worker responsibilities:**

- Run handler state machine to completion (or until `closing` + hand back poll responsibility).
- All broker calls via `BrokerLane` (§7).
- Save state to disk at defined checkpoints (reuse `state_mod.save_state`).
- On unhandled exception: log, set recoverable flag, supervisor may retry with backoff.

### 6.4 What happens to `StopMonitor` class?

**Option A (incremental):** Rename/refactor `StopMonitor` methods into `ExitHandler` modules; supervisor replaces `MonitorRunner`.

**Option B (wrapper):** Keep `StopMonitor` as delegate called from workers (less diff, keeps method corpus).

Recommendation: **Option A** long-term, **Option B** for first migration milestone — document both.
~~~ Lets plan to move to Option A directly.

### 6.5 AlertListener in round-robin

Today: each per-trade thread registers its stop order id.

V3:

- Central **`order_id → path`** map in supervisor.
- On stop replace/cancel: update registration (`_reregister_alert` logic moves to supervisor).
- On fill event: mark slot for **Condition 2** or inject into worker if already running.

**Challenge:** Race between cancel and fill during manual kill — must remain idempotent (existing `handle_stop_order_update` guards for `closing`/`closed`).
~~~ if i understand what race condition you are talking here, I agree if at the same momemnt when I issue kill command market suddenly moves against that trade, stop could get fill before the kill could take action, but if thats the case, and cancel stop order will not be honored by brokerage, and in that situation exit mechanism should route it to wait 30s and then chase the long leg.

### 6.6 Recovery on stop_monitor restart

Today: `_on_load()` / `_recover_closing_on_load()` per thread.

V3: On startup, supervisor builds slots from all `active/*.json` with `open` or `closing`:

- If `spread_close_order_id` set → resume **Condition 3** poll
- If `short_closed_at` set and no spread close → resume **Condition 2** long-chase timer
- If `exit_in_progress` flag persisted? **Not in JSON today** — may need new field or infer from `closing` + mechanism

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
- **`global_semaphore`** = tunable (start 4; max 12?) — ties to TT rate limits.

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

### 8.1 Source of truth

- **Disk JSON** under `trades/active/` is authoritative across processes (dashboard, stop_monitor, entry).
- In-memory slot state is a **cache**; supervisor must **merge** after external writes.

**Existing merge helper:** `_maybe_merge_disk_stop_state()` in `monitor.py` — V3 should centralize merge policy.

### 8.2 Writers

| Writer | When |
|--------|------|
| Supervisor (fast path) | `breach_watch`, heartbeat fields |
| ExitWorker | stop_history, order ids, close fields |
| Entry monitor | entry fills, transition to `open` |
| Dashboard | command files only (not JSON body) |
| `pending_fill_sync` | open order promotion |

### 8.3 File locking

**Today:** No explicit lock; relies on single writer per file per process (one thread per trade). V3 **multiple workers** can touch **different** files safely; same file protected by **one job per path** rule.

**Cross-process:** Entry could write same file while stop_monitor exits — mitigated by status gates (`open` → `closing` → `closed`).

### 8.4 New JSON fields (candidates — needs approval)

| Field | Purpose |
|-------|---------|
| `exit_handler` | `breach` / `stop_filled` / `manual_close` / null |
| `exit_started_at` | observability |
| `close_only_mode` | persisted recovery |

Avoid schema churn unless recovery requires it — can infer from `status` + `close_mechanism` + `spread_close_order_id` for v1.

### 8.5 Fill recording (operator rule)

When `long_close_price` is **null** on closed manual/spread-close trades:

- **Display / PnL default:** treat as **`0.0`**, not open long fill (`close_fills._resolved_long_close_price` today infers — **change required**).
- **Still fix broker parsing** to store actual STC when available.

---

## 9. Touch points outside stop_monitor

| Component | Change needed |
|-----------|---------------|
| `dashboard/templates/index.html` | Optional: parallel `fetch` for kill — low priority |
| `dashboard/server.py` | No change for V3 core |
| `blocks/entry/*` | No change; ensure new open trades register with supervisor |
| `run.py` | Still spawn `blocks/stop/run.py`; no structural change |
| `tests/test_spread_kill.py` | Extend for threaded manual kill + round-robin supervisor |
| `meic0dte/logs/stop_monitor.log` | Already configured in `blocks/stop/run.py` — use for V3 metrics |

---

## 10. Migration phasing (suggested)

| Phase | Deliverable | Risk |
|-------|-------------|------|
| **V3a** | Condition 3 only: `_threaded_spread_close` + `close_only_mode` in **current** per-trade threads | Low — fixes Jul 2 without supervisor rewrite |
| **V3b** | `BrokerLane` + semaphore in existing broker (measure 429) | Medium — needs paper load test |
| **V3c** | Round-robin supervisor replaces `MonitorRunner` threads | High — full regression |
| **V3d** | Enforce `MAX_BREACH_THREADS`, priority queue, metrics | Medium |
| **V3e** | null=0 fill policy + leg parse fix | Low — orthogonal |

**Operator preference from notes:** unified three-condition model + round-robin. **Engineering suggestion:** ship **V3a + V3b** before **V3c** to de-risk.

---

## 11. Challenges requiring discussion

### 11.1 Architecture

1. **Round-robin vs hybrid** — Keep per-trade threads for `closing` state only, round-robin for `open`? Adds complexity; pure model preferred?
2. **Phase 2/3 plugins** — Are they live in production config? Supervisor must call them or explicitly deprecate.
3. **Manual kill vs stop-filled during cancel** — Single unified branch or two code paths?
4. **Killswitch + individual kill** — Dedup when both arrive same cycle?

### 11.2 Broker / TastyTrade

5. **Session thread-safety** — Can one `Session` drive concurrent httpx requests? **Spike required**; do not assume.
6. **Max concurrent orders** — TT account-level limits for SPX 0DTE bursts?
7. **Shared session across processes** — Should entry and stop_monitor share OAuth refresh? (GAP-07 partially addressed per-broker refresh thread.)

### 11.3 Correctness

8. **Long chase after spread close** — Jun 26 ms-50 bug if `short_closed_at` unset while `spread_close_order_id` working — guards must port to V3.
9. **0DTE freeze** — `_broker_actions_frozen()` after market close — supervisor must respect for all handlers.
10. **Partial fills** — `stop_qty_for_state` / partial stop resize — round-robin must not skip.

### 11.4 Observability

11. **Prove parallelism** — Structured logs: `{trade, handler, step, wait_ms, queue_depth}`.
12. **Heartbeat** — `trades/heartbeat.json` today counts threads; V3 should report `{active_slots, active_exit_jobs, broker_in_flight}`.

### 11.5 Testing

13. **Paper test:** 4 simultaneous manual kills — wall time < Jul 2 (~120s)? Target TBD.
14. **Simultaneous breach** on 6+ spreads — verify semaphore + thread cap.
15. **Restart mid-close** — each condition recoverable.

---

## 12. Success metrics

| Metric | Baseline (Jul 2) | Target (TBD with operator) |
|--------|------------------|----------------------------|
| Time from kill command → all stops cancelled | ~3s | ≤ 3s (maintain) |
| Time from kill command → all trades `closed` | ~123s (4 trades) | **Reduce** — depends on fill latency + broker parallelism |
| Supervisor CPU idle | N × sleep(3) threads | One thread, <1ms scan per cycle |
| Concurrent broker HTTP ops | 1 effective | ≥ 4 without 429 storm (tune) |
| Correct exit leg recording | long null | Both legs or null→0 policy |

---

## 13. Reference — key files today

| File | Role |
|------|------|
| `blocks/stop/runner.py` | Per-trade thread supervisor |
| `blocks/stop/monitor.py` | Poll loop, three exit paths, handlers |
| `blocks/stop/phases.py` | Software breach detection |
| `blocks/stop/alerts.py` | TT fill websocket |
| `blocks/stop/run.py` | CLI entry, logging path |
| `brokers/tastytrade_broker.py` | Serialized asyncio broker |
| `blocks/stop/close_fills.py` | Slippage + long price inference |
| `dashboard/server.py` | `/api/close_trade`, `/api/killswitch` |
| `changes/GAP_ANALYSIS.md` | GAP-22 round-robin discussion |

---

## 14. Summary

V3 converges on an operator-aligned model: **one fast round-robin watcher** for all open spreads, **three explicit exit handlers**, and **bounded parallel broker lanes** so independent trades don’t queue behind each other the way they did on Jul 2. The largest unknowns are **TastyTrade session concurrency and rate limits** — those require a measured spike before committing to a broker implementation. The largest **quick win** with lowest risk is threading **manual kill** (Condition 3) and disabling breach watch during close, even before the full supervisor rewrite.

**Next steps (when moving to implementation):**

1. Operator sign-off on precedence rules (§5.1, §5.3) and success metrics (§12).
2. Paper spike: broker parallelism options P1 vs P3 (§7.3).
3. Implement V3a in current architecture OR proceed directly to V3c if spike clean.
