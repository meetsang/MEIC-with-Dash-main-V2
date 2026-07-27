# Launcher Child-Process Restart Backoff — Design Spec

**Status:** PROPOSED (not implemented)  
**Date:** 2026-07-23  
**Incident:** Jul 23 afternoon — TastyTrade IP block / connection timeouts after launcher crash loop  
**Related:** [LIVE_SESSION_2026-07-23.md](LIVE_SESSION_2026-07-23.md), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md), [REST_FILL_SYNC_ENTRY_FIX_DESIGN.md](REST_FILL_SYNC_ENTRY_FIX_DESIGN.md), `run.py`, `common/trading_gate.py`

---

## 1. Problem statement

On **2026-07-23 ~2:47–3:00 PM CT**, the launcher entered a **tight restart loop** after `run.py` was started while TastyTrade REST was already failing (connection timeouts / IP throttle). Every **5 seconds** the main loop detected dead children and immediately respawned:

- **Streamer** (`streaming/publish_tastytrade.py`)
- **Stop monitor** (`blocks/stop/run.py`)

Each respawn creates a **new TastyTrade OAuth session** and hits REST before the child exits again (~5–6s later, exit code 1).

Evidence from that window:

| Metric | Value |
|--------|------:|
| Streamer log files created (`stream_pub_tt_2026-07-23_14*.log`) | **77** |
| Stop-monitor OAuth attempts in `stop_monitor.log` (14:47–15:00) | **~75+** |
| Launcher restart interval | **every 5s** (main loop sleep) |
| Startup REST probe after loop began | `ok=False status=unknown latency_ms=6043` |
| `runtime/rest_metrics_*.json` failures | `ConnectTimeout` on `place_order`, `get_live_orders`, `cancel_order` |
| Explicit HTTP `429` in logs | **none** (block manifested as timeouts) |

**Operator impact:** IP blocked at broker; dashboard REST gate showed **blocked / unknown**; streamer and stop monitor never recovered until the launcher was stopped manually.

This is separate from, but compounded by, **multiple manual `run.py` restarts** earlier in the day (08:00, 13:01, 13:45, 13:55), each doing its own startup OAuth + REST probe.

---

## 2. Background — how child restarts work today

### 2.1 Launcher main loop (`run.py`)

```text
while True:
    if streamer.poll() is not None:
        log.critical('STREAMER exited unexpectedly — restarting ...')
        streamer = start_streamer()          # immediate, no delay

    if stop_mon.poll() is not None:
        log.critical('STOP_MONITOR exited unexpectedly — restarting ...')
        stop_mon = start_stop_monitor()      # immediate, no delay

    if market_data.poll() is not None:
        market_data = start_market_data_recorder()

    _check_running_services_health(...)
    entry_coordinator.tick / entry_runner.tick
    time.sleep(5)
```

**Properties:**

- **No backoff** — failed child → respawn on next loop iteration (≤5s).
- **No circuit breaker** — loop continues indefinitely.
- **No distinction** between “transient blip” (one crash, recover) vs “persistent failure” (API down / IP blocked).
- **Independent counters** — streamer and stop_monitor each restart on their own schedule, doubling OAuth load when both fail together.

### 2.2 What each child does on startup

| Child | REST / OAuth on boot | Typical failure when IP blocked |
|-------|----------------------|----------------------------------|
| **Streamer** | Creates OAuth session; connects DXLink; may read account metadata | Exits ~5s after start; log ends with `Process lock released streamer` |
| **Stop monitor** | Creates OAuth session; reconciles orders/stops | Exits ~5s; log shows `Creating TastyTrade OAuth Session` then lock release |
| **Launcher** (same process) | Startup REST probe via `ProbeCoordinator`; shared broker in launcher PID | Probe `status=unknown`, 6s+ latency; sets `new_risk_latched` |

When REST is unhealthy, **restarting children faster does not help** — it **amplifies** the outage by hammering the same IP with auth and health calls.

### 2.3 Intended vs actual resilience

| Scenario | Intended behavior | Actual behavior (Jul 23) |
|----------|-------------------|---------------------------|
| Streamer crashes once | Restart; resume MQTT | OK when API healthy |
| API / IP block | Back off; surface alert; stop making it worse | Restart every 5s × 2 children ≈ **24 OAuth attempts/minute** |
| Operator restarts `run.py` during outage | Single failed probe; operator waits | Crash loop multiplies damage |
| Stop monitor stale heartbeat | Warn in launcher; child still running | Jul 23 2:30 PM: stale 60–110s while child still alive — **different** from crash loop |

---

## 3. Jul 23 timeline (context)

### 3.1 Morning — normal REST, gate gap for manual

- MEIC tranches **11-00 / 12-00 / 12-30** entered normally.
- **12:30–12:59:** `rest_probe_stale` blocked manual spawn (no probe between tranches) — documented in [LIVE_SESSION_2026-07-23.md](LIVE_SESSION_2026-07-23.md). **No extra REST load** from this (spawn was blocked).

### 3.2 ~1:55–2:46 PM — stable session, degraded health

Session `launcher_2026-07-23_135543.log`:

- Startup REST probe **healthy** (1:55:47).
- **02-00** pre-tranche probe **healthy** (1:58:30).
- **2:30 PM:** stop monitor heartbeat **stale** (60–110s); MQTT **price gates waiting** on some legs.
- **No** `rate_limited` or `REST gate blocked` lines in launcher during this window.

**Operator “REST gate blocked” around 2:30** was likely:

1. Dashboard reading **stale `trading_gate.json`** (`01-45` probe stuck `status_phase: running` from an earlier restart), or
2. Confusion with **red StopMon / streamer health**, not the new-risk latch, or
3. Residual banner from **12:30 `rest_probe_stale`** before the 1:55 restart.

REST API was still answering probes in this window; the afternoon catastrophe started after the **2:47 restart**.

### 3.3 ~2:47–3:00 PM — crash loop (root cause of IP block)

1. Operator started `run.py` while TastyTrade was already timing out.
2. Startup probe: `unknown` (6s).
3. Streamer + stop monitor started, exited code 1 within seconds.
4. Launcher restarted both **every 5s** for ~12 minutes.
5. ~**150+ OAuth sessions** attempted; IP throttle / block.
6. Session ended when operator stopped the launcher (~3:00 PM).

### 3.4 Evening — nothing running

At **~7:15 PM CT** investigation confirmed:

- **No** `python` / `uv` processes.
- Ports **5002** and **1883** free.
- `trading_gate.json`: `rest_status=unknown`, `new_risk_latched=true` from last failed evening probe — **artifact only**, not active traffic.

---

## 4. Why this matters for REST gate policy

The Jul 10–13 **pre-entry REST probe hardening** correctly limits **scheduled** probes (startup + one per tranche). It does **not** cap:

- Launcher **child restart storms**
- Operator **manual restarts** during an outage
- Dashboard **Re-check REST** clicks in a tight loop

A single crash loop can exceed a full day’s intentional probe budget in **minutes**.

---

## 5. Proposed design — child restart backoff + circuit breaker

### 5.1 Goals

1. **Stop amplifying broker outages** — never restart children faster than a configurable minimum interval when failures repeat.
2. **Escalate visibly** — launcher log + dashboard banner when a child is in **circuit-open** state.
3. **Preserve fast recovery** for rare one-off crashes when REST is healthy.
4. **Minimal scope** — change `run.py` supervisor loop only; no change to stop/streamer internals in v1.

### 5.2 Per-child restart state machine

Track separately for `streamer`, `stop_monitor`, `market_data`:

```text
                    ┌─────────────────┐
   child exits ───► │  COOLDOWN       │  wait min_interval (e.g. 30s first time)
                    └────────┬────────┘
                             │ spawn
                             ▼
                    ┌─────────────────┐
         success ──►│  RUNNING        │◄── reset fail_count on stable run (e.g. 120s)
                    └────────┬────────┘
                             │ exit again within window
                             ▼
                    ┌─────────────────┐
   fail_count ≥ N ─►│  CIRCUIT_OPEN   │  stop auto-restart; log CRITICAL; set launcher health flag
                    └─────────────────┘
```

**Suggested defaults (env-tunable):**

| Variable | Default | Meaning |
|----------|--------:|---------|
| `LAUNCHER_CHILD_RESTART_MIN_SEC` | `30` | Minimum wait before respawn after any crash |
| `LAUNCHER_CHILD_RESTART_MAX_SEC` | `300` | Cap for exponential backoff |
| `LAUNCHER_CHILD_RESTART_BACKOFF` | `2.0` | Multiply interval per consecutive failure |
| `LAUNCHER_CHILD_CIRCUIT_FAILS` | `3` | Consecutive crashes → circuit open |
| `LAUNCHER_CHILD_STABLE_SEC` | `120` | Running this long resets fail counter |

**Backoff example** (defaults): fail at T+0 → wait 30s → fail → wait 60s → fail → **circuit open** (no more auto-restart until operator action or next calendar day).

### 5.3 REST-aware guard (v1.1 — recommended with backoff)

Before respawning a REST-touching child (`streamer`, `stop_monitor`), check:

```python
if trading_gate.rest_status in ('unknown', 'unavailable', 'rate_limited'):
    if not fresh_successful_probe_within(REST_READY_MAX_AGE_SEC):
        defer_restart('rest_unhealthy')
```

If REST is globally unhealthy, **do not** spawn children that will immediately OAuth. Log once per minute:

```text
LAUNCHER_CHILD_RESTART_DEFERRED service=streamer reason=rest_unhealthy
```

Operator can still use dashboard **Re-check REST** after IP cools down, then **Resume** or restart launcher once.

### 5.4 Circuit-open behavior

When circuit opens for `stop_monitor`:

- **Do not** call `start_stop_monitor()` again in the loop.
- Set `runtime/launcher_health.json` (new small file) e.g.:

```json
{
  "streamer": {"state": "circuit_open", "fail_count": 5, "last_exit_code": 1},
  "stop_monitor": {"state": "circuit_open", "fail_count": 5, "last_exit_code": 1}
}
```

- Dashboard system health: show **“Streamer circuit open — manual restart required”** (red).
- **Existing open positions:** stops already at broker remain; software breach path is **degraded** until stop monitor recovers — same as today when stop monitor is dead, but without API hammering.

When circuit opens for `streamer`:

- MQTT prices go stale; stop monitor may log `waiting_mqtt` — expected until streamer recovers.

**Recovery paths:**

1. Operator stops `run.py`, waits for IP cooldown, starts fresh `run.py`.
2. (Future) Dashboard button: **“Reset child circuits”** after successful REST probe.

### 5.5 Do not auto-restart on session end

Already correct: when main loop breaks at 3 PM CT, `finally` terminates children once. No change.

### 5.6 Related hardening (same incident, separate PRs)

| Item | Priority | Notes |
|------|----------|-------|
| **Stuck `probes_by_tranche` record** (`01-45` `status_phase: running`) | P2 | Write completion on probe finish; dashboard should not show phantom “pending” |
| **Manual restart guidance** | Ops | Do not start `run.py` repeatedly while probes return `unknown` |
| **Dashboard Re-check debounce** | P2 | Ignore repeat clicks within 30s |
| **Shared OAuth for streamer** | P3 | Larger refactor; reduces per-restart auth |

---

## 6. Implementation plan

### Phase 1 — Backoff + circuit breaker (`run.py` only)

1. Add `_ChildRestartTracker` dataclass (per service: `fail_count`, `next_restart_mono`, `circuit_open`, `last_exit_code`).
2. Replace immediate `start_*()` calls with `_maybe_restart_child(name, starter_fn, proc)`.
3. On successful `poll() is None` for `STABLE_SEC`, reset tracker.
4. Write `runtime/launcher_health.json` each loop when any child not RUNNING.
5. Log lines:
   - `LAUNCHER_CHILD_RESTART_SCHEDULED service=streamer in_sec=30 fail_count=2`
   - `LAUNCHER_CHILD_CIRCUIT_OPEN service=stop_monitor fail_count=3`
   - `LAUNCHER_CHILD_RESTART_DEFERRED service=streamer reason=rest_unhealthy`

### Phase 2 — Dashboard visibility

1. Expose `launcher_health` in `/api/summary` `system_health`.
2. Red health pill when `circuit_open`.
3. Optional: link to this doc from banner text.

### Phase 3 — REST deferral

1. Import `read_state` from `common.trading_gate`.
2. Skip child restart when global REST unhealthy (§5.3).

### Tests

| Test | Assert |
|------|--------|
| `test_child_restart_respects_min_interval` | Two crashes within 5s → only one respawn before min_interval |
| `test_child_circuit_opens_after_n_failures` | Third crash → no fourth `Popen` |
| `test_stable_run_resets_fail_count` | Run 120s mock → fail counter zeroed |
| `test_rest_unhealthy_defers_restart` | Gate `unknown` → no `start_streamer` call |

---

## 7. Operator playbook (until implemented)

1. **If REST probe returns `unknown` or times out >5s:** do **not** restart `run.py` in a loop. Wait **30–60+ minutes** for IP cooldown.
2. **If launcher log shows repeating** `STREAMER exited unexpectedly — restarting` **more than 3 times:** **Ctrl+C** immediately.
3. **Distinguish health signals:**
   - `REST gate blocked` / `new_risk_latched` → probe / latch (blocks **new entries**)
   - `StopMon heartbeat stale` → child hung or dead (blocks **software breach** path; exchange stops may still work)
4. **One** clean `run.py --one-day` start after cooldown; avoid evening restarts “just to check.”
5. Do not spam dashboard **Re-check REST** — each click is a live API call.

---

## 8. Success criteria

- [ ] A simulated child crash loop produces **≤3 OAuth attempts** per service, then circuit open.
- [ ] No more than **1 child restart per 30s** per service under repeated failure.
- [ ] Jul 23-style afternoon (IP block) no longer generates **70+** streamer log files in 12 minutes.
- [ ] Dashboard shows clear **circuit open** state when auto-restart has stopped.
- [ ] Normal single crash (API healthy) still recovers within **30–60s** without operator action.

---

## 9. Open questions

1. Should **market_data** share the same circuit bucket as streamer (both MQTT-only, no OAuth)? **Proposal:** market_data gets backoff but no REST deferral.
2. Should circuit auto-close after **successful startup probe** without full launcher restart? **Proposal:** yes in v1.1 via dashboard “Reset circuits” after Re-check succeeds.
3. Should we **kill launcher entirely** when both streamer and stop_monitor circuits open? **Proposal:** no — keep entry monitor idle; operator may still need dashboard for PnL.

---

*Drafted from Jul 23 live investigation — crash loop 14:47–15:00 CT, `launcher_2026-07-23_144705.log`, 77× `stream_pub_tt_2026-07-23_14*.log`, `runtime/trading_gate.json`, `runtime/rest_metrics_5064.json`.*
