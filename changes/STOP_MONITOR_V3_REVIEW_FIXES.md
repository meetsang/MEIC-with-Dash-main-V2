# Stop Monitor V3 — External Review & Fix Tracker

**Date:** 2026-07-05  
**Source:** Static code review (ChatGPT) vs shipped implementation + design doc  
**Related:** [STOP_MONITOR_V3_DESIGN.md](STOP_MONITOR_V3_DESIGN.md) §6.7, §8.1, §12.4

This document tracks findings from the post-ship review, maps each item to design intent, and records fix status. **Runtime certification still requires Monday live session.**

---

## Summary verdict

| Category | Count | Notes |
|----------|-------|-------|
| **Fixed in this pass** | 2 | Recovery bug + mtime discovery |
| **Table for post-live** | 4 | Broker I/O, priority queue, chain cache, breach restart |
| **Confirmed OK** | 5 | Manual kill, command claim, config, V2.9, hybrid bridge |

---

## Fix status legend

| Status | Meaning |
|--------|---------|
| **FIXED** | Patched in repo; test added where applicable |
| **TABLED** | Deferred until live C1/C2 / scale testing this week |
| **BY DESIGN** | Intentional hybrid; documented in design §16.6 |
| **OK** | Review finding validated; no change needed |

---

## F-1 — Restart recovery: `open + close_only_mode` stranded (MUST FIX)

**Review finding:** After manual kill command is claimed and persisted, a stop_monitor restart before `status=closing` leaves the trade in `open + close_only_mode`. Supervisor logged `recover_route=resume_exit_handler` but did not re-enqueue `ManualKillHandler`. `_scan_slot()` returned early without resuming exit work.

**Design reference:** §6.7 Recovery on stop_monitor restart; §8.5 `close_only_mode` must survive restart and **skip breach** while **resuming** the correct exit handler.

**Risk:** Safety state becomes an **unwatched open trade** after restart.

**Status:** **FIXED** (2026-07-05)

**Patch:** `_scan_slot()` now re-enqueues `ManualKillHandler` when:
- `close_only_mode` or manual `exit_handler`, and
- `status == 'open'`, and
- no active exit job on that path

**Test:** `tests/test_v3_paper_scenarios.py::TestRestartMidClose::test_supervisor_resumes_manual_kill_on_open_close_only_restart`

---

## F-2 — Mtime-gated cache not effective in discovery (SHOULD FIX)

**Review finding:** `_discover_slots()` called `load_state()` for every active JSON every supervisor cycle (~4/sec), then `merge_disk_state()`. At 40 slots this recreates the ~160 JSON reads/sec problem §8.1 was meant to avoid.

**Design reference:** §8.1 — steady-state model: *read once into cache, merge when mtime changes*; only `stat().st_mtime` per slot each cycle.

**Status:** **FIXED** (2026-07-05)

**Patch:**
- **Existing slots:** `merge_disk_state(slot)` only (loads JSON when mtime changes)
- **New paths:** single `load_state()` when path first seen
- **Removed/closed:** dropped from `_slots` when merge shows ineligible status
- **Pending fill sync:** throttled to `SLOW_INTERVAL` (10s), not every cycle

**Follow-up (TABLED):** Add metric/logging for cache hit rate at 40-slot paper load test.

---

## T-1 — Broker parallelism not fully proven (TABLED)

**Review finding:** `TastyTradeBroker` still uses one asyncio loop; `_run()` blocks on `future.result()`. V3-0 spike showed ~2.7× speedup at lane=6 for read-only probes, but full exit-path concurrency under load is not certified.

**Design reference:** §7.3 spike required before assuming P1 works; §7.4 rate limits.

**Status:** **TABLED** — post-live this week

**Action items:**
1. Re-run `scripts/v3_broker_spike.py` during market hours with live order book
2. Log `v3_exit` `wait_ms` during 4 simultaneous manual kills
3. Watch for 429 in `stop_monitor.log`
4. Tune `STOP_BROKER_LANE_SIZE` up only if clean

---

## T-2 — Supervisor-thread broker I/O (TABLED / BY DESIGN hybrid)

**Review finding:** Alert fill drain, slow REST sync, and `_ensure_stop_for_filled_qty` can call broker from the supervisor scan thread, blocking the round-robin loop.

**Design reference:** §6 — supervisor should stay fast; broker work in exit workers.

**Status:** **TABLED** — acceptable for 2–10 slot live rollout; refactor after Monday

**Current bridge (intentional):**
| Call site | Cadence | Mitigation |
|-----------|---------|------------|
| `_drain_alert_fills` | On alert event | Enqueues C2 worker; one `get_order_status` |
| `_slow_broker_sync` | Every `SLOW_INTERVAL` (10s) per slot | Not every 0.25s cycle |
| `_ensure_stop_for_filled_qty` | When stop missing | Rare after entry handoff |

**Future:** Move slow sync + alert confirm into `BrokerLane` worker jobs.

---

## T-3 — Option chain rebuild storm (TABLED)

**Review finding:** `_get_option()` full SPX chain fetch on cache miss; simultaneous exits could trigger parallel chain rebuilds.

**Design reference:** §7.5 — mutex + warm cache at session start.

**Status:** **TABLED** — pre-market chain warm or mutex after live observation

---

## T-4 — ExitWorkerPool manual priority queue (TABLED)

**Review finding:** `_manual_priority` / `_fifo` lists existed but were unused; all jobs start threads immediately and compete on semaphore.

**Design reference:** §6.3 — manual kill priority under killswitch burst.

**Status:** **TABLED** — documented in `exit_pool.py` docstring; not a blocker for 2–4 trade manual kill

**Reality at current scale:** Semaphore cap (6 broker ops) + one job per path prevents double-close. Under 40-trade killswitch, blocked threads could accumulate — implement real priority queue if Monday testing shows contention.

---

## T-5 — Breach handler restart recovery (TABLED)

**Review finding:** F-1 fix covers **manual** exit handlers. If restart occurs mid-**breach** (`exit_handler=breach_*`, `status=open`), no automatic re-enqueue of `SoftwareBreachHandler` yet.

**Design reference:** §6.7 — resume appropriate handler by `exit_handler`.

**Status:** **TABLED** — lower priority; breach exits usually reach `closing` quickly; validate live if seen

---

## Confirmed OK (no change)

| Item | Location | Notes |
|------|----------|-------|
| Manual kill pipeline | `handlers/manual_kill.py` | Cancel → C2 if filled; quote fallback; spread close |
| Command claiming | `command_claim.py` | Atomic rename, persist, archive |
| V2.9 null-long policy | `close_fills.py` | manual/admin → `0.0` |
| Feature flag | `run.py` | `v2` default; `v3` → StopSupervisor |
| Live defaults | `v3/config.py` | cycle 0.25s, lane 6, stall 120s |
| V2 rollback | `monitor.py` | `close_only_mode` honored in `_poll_once` |

---

## Priority order (updated)

```text
DONE  F-1  open + close_only_mode → re-enqueue ManualKillHandler
DONE  F-2  mtime-gated discovery + throttle pending_fill_sync

MON   Live C1/C2 observation (design §16.8)
WEEK  T-1  Broker parallelism metrics under real exit load
WEEK  T-2  Move supervisor broker calls to workers (if scan latency observed)
WEEK  T-3  Chain cache mutex / pre-warm
LATER T-4  Real priority queue (if killswitch burst tested)
LATER T-5  Breach handler restart recovery
```

---

## Useful commands (regression)

```powershell
# F-1 recovery test
python -m pytest tests/test_v3_paper_scenarios.py::TestRestartMidClose -v

# Full V3 suite
python -m pytest tests/test_v3_*.py -v

# Broker spike (T-1)
python scripts/v3_broker_spike.py --order-ids <ids>
```

---

## Cross-reference to design doc

After this pass, update [STOP_MONITOR_V3_DESIGN.md](STOP_MONITOR_V3_DESIGN.md) §16.7 warning list:

- ~~Restart mid manual-kill before `closing`~~ → **fixed F-1**
- mtime cache → **fixed F-2** for steady-state; profile at 40 slots still pending

---

*Next update: after Monday live session — mark T-1/T-2/T-5 based on observed behavior.*
