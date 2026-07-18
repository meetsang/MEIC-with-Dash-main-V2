# Live Session Notes — Jul 13, 2026

**Status:** Morning incident — **11-00 MEIC did not fire** (investigated ~11:32 CT).  
**Related:** [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md) (**open fix spec**), [RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md](RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md), [LIVE_SESSION_2026-07-09.md](LIVE_SESSION_2026-07-09.md) (01-45 cooldown miss), [LIVE_SESSION_2026-07-01.md](LIVE_SESSION_2026-07-01.md) (missed wake)

---

## Operator question — why did 11 o'clock MEIC not trigger?

### Short answer

The **11-00 tranche never entered**. Session rows are still `pending`, no `11-00_*` trade JSON was created, and the launcher log has **no** `Spawned entry worker for 11-00_P/C` lines. Infrastructure (launcher, streamer, stop_monitor, MQTT) was **up** during the **10:59–11:05 CT** entry window, so this was **not** a full-bot-down or PC-sleep miss. The most likely failure is in the **launcher entry monitor path** — specifically the **pre-entry REST gate** blocking or stalling the main loop before workers spawn.

---

## Day summary (morning)

| Tranche | Side | Entry | Close | Notes |
|---------|------|-------|-------|-------|
| 11-00 | P | **did not fire** | — | CSV `state=pending` |
| 11-00 | C | **did not fire** | — | CSV `state=pending` |
| 12-00 | — | pending | — | Not yet due at observation time |
| 02-00 | P/C | paused | — | Operator pre-paused in session CSV |

---

## Timeline (CT)

| Time | Event | Source |
|------|-------|--------|
| **08:00:10** | Launcher started (`run.py`, live TastyTrade) | `logs/launcher_2026-07-13_080010.log` |
| **08:00:15** | Startup REST probe OK (`healthy`, 91 ms) — **last probe recorded all morning** | launcher log + `runtime/trading_gate.json` |
| **08:30:00** | Streamer, market_data, stop_monitor started | launcher log |
| **08:30:10** | Strategies loaded: MEIC_IC + MANUAL_SPREAD | launcher log |
| **10:05:56** | `STOP_MONITOR MQTT — cache health file absent` (rate-limited warning) | launcher log |
| **10:20:08** | Last market_data index tick written | `logs/market_data_2026-07-13_083003.log` |
| **10:20:20** | Last `SPX_polls.csv` / `GLD_polls.csv` row | `data/2026-07-13/` |
| **10:20:22** | **market_data subprocess exited** (`Process lock released market_data`) | market_data log |
| **10:59:00** | Streamer receiving live SPX option quotes | `logs/stream_pub_tt_2026-07-13_083003.log` |
| **10:59–11:05** | **Expected 11-00 entry window — no spawn, no orders** | session CSV + launcher log |
| **11:05:00** | Streamer still live through window end | stream_pub log |
| **~11:32** | Observation: launcher PID 7620 still running; stop_monitor heartbeat healthy | process list + `trades/heartbeat.json` |

---

## Evidence — 11-00 did not fire

| Check | Result |
|-------|--------|
| `trades/session/MEIC_IC_2026-07-13.csv` rows `11-00_P` / `11-00_C` | `state=pending`, `paused=false`, window `10:59–11:05` |
| `trades/active/MEIC_IC/11-00_*` | **None** |
| Launcher log `Spawned entry worker` | **Absent** |
| `runtime/trading_gate.json` | `new_risk_latched=false`, `rest_status=healthy` |
| `runtime/broker_cooldown.json` | **Absent** (no cooldown) |
| `last_successful_probe_epoch` | **08:00:15 only** — no `pre_entry` probe ever recorded |

---

## Root cause analysis

### What we ruled out

| Hypothesis | Why unlikely |
|------------|--------------|
| Tranche paused | CSV `paused=false` for 11-00 |
| Gate latched / cooldown | `new_risk_latched=false`; no `broker_cooldown.json` |
| PC asleep during 11-00 window | Streamer log shows continuous quotes at **10:59** and **11:05** |
| Stop monitor down | Heartbeat `engine=v3`, `loop_count` 41k+ at 11:30 |
| Wrong session date / missing CSV | `MEIC_IC_2026-07-13.csv` present with 12 rows |

### Primary hypothesis — pre-entry REST gate on launcher main thread

Entry spawning goes through `EntryMonitorRunner._gate_allows_spawn()` → `evaluate_new_risk_gate(require_fresh_probe=True)` (`blocks/entry/runner.py`).

With `NEW_RISK_GATE_ENABLED=true` (default) and `REST_PROBE_BEFORE_NEW_ENTRY=true` (default):

1. At **08:00:15** the only REST probe of the day succeeded (`startup`).
2. By **10:59**, that probe is **>60s stale** (`REST_READY_MAX_AGE_SEC`).
3. On the **first** spawn attempt for `11-00_P`, the runner must call `run_rest_probe(get_broker(), source='pre_entry')` **on the launcher main thread** before `Spawned entry worker` is logged.
4. `get_broker()` creates a **new** `TastyTradeBroker` + OAuth session in the **launcher process** (separate from stop_monitor’s shared broker on PID 7744).
5. **No `pre_entry` probe** appears in `trading_gate.json` and **no spawn log** was written → the main loop likely **never completed** the pre-entry probe + spawn sequence during the window.

Possible stall points:

- `get_broker()` / `TastyTradeBroker.__init__` (new asyncio loop + account bootstrap)
- `probe_orders_rest()` → `future.result(timeout=10)` waiting on broker loop
- `_PROBE_LOCK` held while a hung probe blocks subsequent ticks

This matches Jul 9 **01-45** pattern (cooldown blocked pricing) in spirit — **new-risk infrastructure prevented entry** — but today there is **no latch** and **no cooldown file**, pointing to a **hang or silent failure before probe results are recorded** rather than an explicit latched block.

### Secondary — market_data died; launcher did not log restart

| Fact | Implication |
|------|-------------|
| market_data exited **10:20:22** | `SPX_polls.csv` / ladder snapshots stop updating after **10:20** |
| No `MARKET_DATA exited unexpectedly` in launcher log | Launcher main loop may not be completing its 5s health cycle (same main-thread stall), **or** child handle state is wrong |
| No `market_data.lock` after exit | Recorder is down; not auto-resumed at observation time |

market_data loss does **not** directly block MEIC entry (entry uses broker REST + MQTT), but it is a **canary** that launcher supervision was unhealthy before 11:00.

### Design gap — no catch-up for missed windows

`_should_fire_meic()` requires `row.is_in_window(now_time)` at tick time (`blocks/entry/runner.py`). If the main loop is blocked for the entire **10:59–11:05** window, rows stay `pending` forever with **no retry** — same class of problem as Jul 1 missed wake, but with a **running** launcher.

---

## Contributing noise

| Issue | Detail |
|-------|--------|
| **MQTT port conflicts** | Streamer log: repeated `WinError 10048` on MQTT publish (socket already in use) — suggests duplicate MQTT clients/publishers on host |
| **Stale REST probe all morning** | Gate file shows healthy status from **08:00** only; pre-entry path never refreshed |
| **02-00 pre-paused** | Operator intent; unrelated to 11-00 |

---

## Observations table

| Time (CT) | Event | Notes |
|-----------|-------|-------|
| 08:00 | Launcher + dashboard up | PID 7620; live broker |
| 08:30 | Streamer + stop_monitor V3 | Streamer session `20260713-083004-18252-394250` |
| 10:20 | market_data exit | Index/ladder CSV recording stops |
| 10:59–11:05 | **11-00 window missed** | Streamer live; entry monitor silent |
| ~11:32 | Bot still “running” | `bot_status.json` `state=running` since 08:00:15 |

---

## Immediate operator actions

1. **Restart launcher** (`python run.py` or Task Scheduler one-day) — clears a potentially stuck main thread.
2. On dashboard: **Re-check REST** → if latched, **Resume New Entries** (only when REST healthy and no `cooldown_blind` trades).
3. If you still want 11-00 exposure today: **widen** `11-00_P/C` `entry_window_end` in Session Plan (e.g. to `11:30`) **or** run off-hours `python run.py --tranche-now --lot 11-00 --force` (understand this bypasses schedule waits).
4. Confirm **market_data** restarts after launcher recycle (`logs/market_data_*.log` should show new ticks).
5. Keep machine **awake** through tranche windows; disable sleep on AC power.

---

## Follow-up / fix candidates

Full design + acceptance criteria: **[PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md)**

| Priority | Item | Status |
|----------|------|--------|
| **P0** | Pre-entry probe coordinator (1 startup + 1/tranche, shared P/C, non-blocking) | **IMPLEMENTED** on `fix/pre-entry-rest-probe-hardening` — [design](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md) |
| **P0** | CRITICAL `TRANCHE_MISSED` when window ends with no spawn | **IMPLEMENTED** — same branch |
| **P0** | **Windows Avast SSLKEYLOGFILE** — OPENSSL crash | **FIXED** (`common/win_ssl_env.py` + entry scripts; Avast File/Folder exception) |
| **P1** | Investigate why **market_data** exit at 10:20 did not produce launcher restart log | OPEN |
| **P2** | Resolve MQTT **WinError 10048** | OPEN |

---

## Sign-off checklist (fill later)

| Item | Pass / fail | Notes |
|------|-------------|-------|
| 11-00 root cause confirmed | | Pre-entry gate / main-thread stall suspected |
| Launcher restarted | | |
| 12-00 tranche fired | | |
| market_data recording restored | | |
| REST probe fresh before entries | | `last_successful_probe_epoch` < 60s |

---

## Evidence file index

| Artifact | Path |
|----------|------|
| Launcher log | `logs/launcher_2026-07-13_080010.log` |
| Session plan | `trades/session/MEIC_IC_2026-07-13.csv` |
| Trading gate | `runtime/trading_gate.json` |
| market_data log | `logs/market_data_2026-07-13_083003.log` |
| Streamer log (10:59–11:05) | `logs/stream_pub_tt_2026-07-13_083003.log` |
| Stop monitor heartbeat | `trades/heartbeat.json` |
