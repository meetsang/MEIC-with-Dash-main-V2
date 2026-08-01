# Stop Monitor V3 — Review & Live Observation Guide

**Date:** 2026-07-05 (updated second review pass)  
**Sources:** Static code review (ChatGPT, two passes) vs shipped implementation + design doc  
**Related:** [STOP_MONITOR_V3_DESIGN.md](STOP_MONITOR_V3_DESIGN.md) §6.7, §8.1, §12.4, §16  
**Incident (Jul 6):** [STOP_MONITOR_V3_INCIDENT_2026-07-06.md](STOP_MONITOR_V3_INCIDENT_2026-07-06.md) — false breach + duplicate close; F-3–F-7 proposed  
**Latest commit:** F-1/F-2 fixes in `9c5d148`

This document tracks review findings, fix status, **this week's live observation plan**, and **end-of-day passive audits** when the session looked normal.

**Test suite note:** 254 tests pass locally (`python -m pytest tests/ -q`). GitHub has no CI workflow — regression is local pytest before deploy.

---

## Current verdict (second review pass)

**Proceed with V3 live observation this week.** F-1 and F-2 are fixed in code and covered by regression tests. Remaining items (T-1–T-5) are **observation targets**, not blockers, as long as:

- Only **one** stop_monitor process runs
- `STOP_MONITOR_ENGINE=v2` rollback stays ready
- You do **not** force dangerous tests on production spreads (no re-kill same spread for C2 proof)

| Path | Validation status |
|------|-------------------|
| **C3 Manual kill** | Live-validated Jul 4 (dual CCS); F-1 restart fix added Jul 5 |
| **C2 Exchange stop fill** | Code + paper tests; **needs natural market-hour event** |
| **C1 Software breach** | Code + paper tests; **needs natural breach during market hours** |
| **Quote fallback** | Live-validated (`broker_rest` after hours) |
| **V3-0 broker spike** | ~2.7× vs serial at lane=6; no 429 in after-hours probe |

---

## Fix status legend

| Status | Meaning |
|--------|---------|
| **FIXED** | Patched in repo; test added where applicable |
| **TABLED** | Observe live this week; patch only if evidence warrants |
| **BY DESIGN** | Intentional hybrid; see design §16.6 |
| **OK** | Review validated; no change needed |

---

## Fixed issues

### F-1 — Restart recovery: `open + close_only_mode` stranded

**Finding:** After manual kill claimed + persisted, restart before `status=closing` left trade unwatched — supervisor skipped breach but did not resume `ManualKillHandler`.

**Status:** **FIXED** (2026-07-05, commit `9c5d148`)

**Patch:** `_scan_slot()` re-enqueues `ManualKillHandler` when `open + close_only_mode/manual exit_handler` and no active exit job.

**Test:** `tests/test_v3_paper_scenarios.py::TestRestartMidClose::test_supervisor_resumes_manual_kill_on_open_close_only_restart`

**Second review:** Confirmed in `supervisor.py` + review doc + test on GitHub.

---

### F-2 — Mtime-gated cache not effective in discovery

**Finding:** `_discover_slots()` called `load_state()` every cycle for every slot (~160 JSON reads/sec at 40 slots).

**Status:** **FIXED** (2026-07-05, commit `9c5d148`)

**Patch:**
- Existing slots: `merge_disk_state(slot)` only (JSON read when mtime changes)
- New paths: single `load_state()` on first discovery
- `pending_fill_sync`: throttled to `SLOW_INTERVAL` (10s), not every 0.25s cycle

**Follow-up (TABLED):** Cache hit-rate metric at 40-slot paper load — not needed for 2–10 live trades.

**Second review:** Confirmed in `supervisor.py`.

---

## Tabled items — observe this week

### Observation matrix

| ID | What you are testing | Natural trigger | What “good” looks like | What “bad” looks like |
|----|----------------------|-----------------|------------------------|------------------------|
| **T-1** | Broker parallelism under exit load | Kill Selected on **2–4 open CCS** once; or `v3_broker_spike.py` in market hours | Cancels + spread closes submitted within a few seconds; `broker_in_flight` > 1 in heartbeat during burst; no 429 | >30s gap between first and last cancel; 429 lines in log; all work strictly serial |
| **T-2** | Supervisor scan lag from broker I/O on scan thread | Normal session with several open trades; slow REST sync / alert fill windows | `heartbeat.json` `loop_count` advances steadily (~4/sec); no multi-second gaps in `ts` | Heartbeat `ts` frozen >30s while trades open; supervisor appears stuck |
| **T-3** | Option chain cold-cache storm | Restart stop_monitor near open, then 2+ exits before chain warm | First exit only modestly slower than later; no multi-minute stall | Several exits all pause together on first broker call; log shows repeated chain fetches |
| **T-4** | Manual-kill priority under burst | Killswitch or Kill Selected on **6–12+** trades (optional) | All kills eventually complete; no duplicate closes | Many threads blocked; memory climb; kills complete but very slow tail |
| **T-5** | Breach handler restart recovery | Rare: restart while `exit_handler=breach_*` and `status=open` | N/A — avoid restarting mid-breach unless testing | After restart, breach exit stuck open with no new handler |

### T-1 detail — broker parallelism

**Review finding:** `TastyTradeBroker` uses one asyncio loop; V3-0 spike showed ~2.7× read-only speedup at lane=6 — full exit-path concurrency not certified.

**Actions this week:**
1. Re-run `python scripts/v3_broker_spike.py --order-ids <ids>` during market hours
2. On manual kill burst, compare log timestamps (see § Per-trade timeline below)
3. Grep log for `429`
4. Tune `STOP_BROKER_LANE_SIZE` up only if clean

---

### T-2 detail — supervisor-thread broker I/O

**Status:** **BY DESIGN hybrid** for 2–10 slots; refactor if T-2 EOD audit fails.

| Call site | Cadence |
|-----------|---------|
| `_drain_alert_fills` | On alert; one `get_order_status` then enqueue C2 worker |
| `_slow_broker_sync` | Every 10s per slot (not every 0.25s) |
| `_ensure_stop_for_filled_qty` | When stop missing (rare post-entry) |

---

### T-3 — Option chain cache

**Status:** **TABLED** — pre-market chain warm or mutex if EOD audit shows cold-start delay pattern.

---

### T-4 — ExitWorkerPool priority

**Status:** **TABLED** — documented in `exit_pool.py`: no real priority queue; semaphore bounds concurrent broker ops. Fine for 2–4 kills.

---

### T-5 — Breach restart recovery

**Status:** **TABLED** — add F-1-style regression for `open + exit_handler=breach_*` after live breach observed. **Do not restart stop_monitor intentionally during first seconds of a breach.**

---

## Confirmed OK (no change)

| Item | Location |
|------|----------|
| Manual kill pipeline | `handlers/manual_kill.py` |
| Command claiming | `command_claim.py` |
| V2.9 null-long policy | `close_fills.py` |
| Feature flag | `run.py` — `v2` default, `v3` → StopSupervisor |
| Live defaults | `v3/config.py` — cycle 0.25s, lane 6, stall 120s |
| V2 rollback | `monitor.py` — honors `close_only_mode` |

---

## Active session checklist (when something is happening)

### Before market

```text
1. Confirm only one stop_monitor process is running.
2. STOP_MONITOR_ENGINE=v3 in local .env (never commit .env).
3. Keep STOP_MONITOR_ENGINE=v2 rollback command ready.
4. Note starting loop_count in trades/heartbeat.json.
```

### During session (only if you kill, breach, or stop fills)

```text
1. Let existing closing trades finish — do not re-kill same spread.
2. C2 natural: stop filled → status closing → ~30s → long chase → closed.
3. C1 natural: breach handler → cancel stop → short limit if needed → long chase.
4. Manual kill: record wall time from dashboard click → log "Claimed manual close".
5. Grep live: v3_exit | Exit job started | Claimed manual close | 429 | exit_stalled | CRITICAL
```

### After an event (kill / breach / stop fill)

```text
1. No duplicate spread-close or double-close attempts in log.
2. Trade JSON: status closed (or closing with working spread_close_order_id).
3. long_close_price: real broker value preferred; 0.0 only on manual/admin if STC unknown (V2.9).
4. close_only_mode cleared or trade moved to history.
5. heartbeat loop_count still advancing.
```

---

## End-of-day passive audit (session looked normal)

Use this when **nothing looked wrong during the day** — no kills, breaches, or stop fills you noticed. Takes ~2 minutes. Confirms V3 ran healthy without requiring intraday watching.

### 1. Heartbeat — supervisor alive

```powershell
cd MEIC-with-Dash-main-V2
Get-Content trades/heartbeat.json | ConvertFrom-Json | Format-List
```

| Field | Pass if |
|-------|---------|
| `engine` | `v3` |
| `loop_count` | Large number (thousands after full session); grew since morning |
| `target_cycle_sec` | `0.25` |
| `active_slots` | Matches your open + closing trade count |
| `broker_in_flight` | Usually `0` when idle; briefly >0 during exits is OK |
| `broker_lane_max` | `6` (unless you changed env) |

**Fail signal:** `engine` missing/`v2`, or `loop_count` barely moved all day.

---

### 2. Log — no silent failures

```powershell
Select-String -Path meic0dte/logs/stop_monitor.log -Pattern "CRITICAL|exit_stalled|429|Supervisor cycle failed|Traceback" |
  Select-Object -Last 20
```

**Pass:** No matches, or only known benign lines you can explain.

**Fail signals:**
- `Exit stalled on` → stuck exit; check trade JSON `exit_stalled`, `exit_error`
- `429` → rate limit; note time and consider lowering `STOP_BROKER_LANE_SIZE`
- `Supervisor cycle failed` / `Traceback` → bug or broker outage

Optional — confirm V3 ran (even quiet day):

```powershell
Select-String -Path meic0dte/logs/stop_monitor.log -Pattern "StopSupervisor V3 watching" | Select-Object -Last 1
```

---

### 3. Active trade JSON — no orphaned safety states

```powershell
Get-ChildItem trades/active -Recurse -Filter *.json | ForEach-Object {
  $j = Get-Content $_.FullName | ConvertFrom-Json
  [PSCustomObject]@{
    File = $_.Name
    Status = $j.status
    CloseOnly = $j.close_only_mode
    ExitHandler = $j.exit_handler
    ExitStalled = $j.exit_stalled
    ExitError = $j.exit_error
  }
} | Format-Table -AutoSize
```

**Pass (normal open day):**
- All rows `Status=open`, `CloseOnly` empty/false, no `ExitStalled`, no `ExitError`

**Fail signals:**
- `open` + `CloseOnly=true` + no `spread_close_order_id` for hours → F-1 regression or stuck kill (should auto-resume on next scan after fix)
- `ExitStalled=true` or `ExitError=missing_quotes` → needs operator review

---

### 4. Closed trades today — fill quality (if any closed)

If trades moved to `trades/history/` or `status=closed` in active:

```powershell
# Adjust path/glob for your strategy folder
Get-ChildItem trades/history -Recurse -Filter *.json -ErrorAction SilentlyContinue |
  Where-Object { $_.LastWriteTime.Date -eq (Get-Date).Date } |
  ForEach-Object {
    $j = Get-Content $_.FullName | ConvertFrom-Json
    [PSCustomObject]@{
      Lot = $j.lot
      Mechanism = $j.close_mechanism
      ShortClose = $j.short_close_price
      LongClose = $j.long_close_price
    }
  } | Format-Table -AutoSize
```

**Pass:**
- `long_close_price` populated when broker returned leg fills
- Manual/admin close with null long → PnL uses `0.0` fallback (V2.9), not open-fill inference

---

### 5. T-1 / T-2 / T-3 EOD signals (passive)

Even on a quiet day, these fields support tabled items without forced tests:

| ID | Passive EOD check | Pass |
|----|-------------------|------|
| **T-1** | No `429` in log; heartbeat `broker_in_flight` never stuck high | Clean |
| **T-2** | Heartbeat `loop_count` delta ÷ session seconds ≈ 4/sec (±20%) | Scan loop healthy |
| **T-3** | No log lines showing repeated slow first-exit pattern after restart | No cold-cache storm |
| **T-4** | N/A on quiet day | — |
| **T-5** | No `open` + `exit_handler` starting with `breach_` | — |

**T-2 quick math:**

```powershell
$hb = Get-Content trades/heartbeat.json | ConvertFrom-Json
# Compare loop_count now vs a morning snapshot you saved, divide by elapsed seconds
# Expect ~4 loops/sec with TARGET_CYCLE_SEC=0.25
```

Save morning heartbeat snapshot (optional):

```powershell
Copy-Item trades/heartbeat.json trades/heartbeat_morning.json
```

---

## Per-trade exit timeline (log + JSON mapping)

When analyzing a kill or exit, map delays to pipeline stages. ChatGPT suggested explicit timestamps — here is what **actually exists today**:

| Stage | Log grep | Trade JSON field |
|-------|----------|------------------|
| Command claimed | `Claimed manual close` | `exit_started_at`, `exit_last_step=manual_kill_claimed` |
| Exit job accepted | `Exit job started` + `v3_exit` `"step":"job_started"` | — |
| Cancel stop start | `v3_exit` / `exit_last_step=cancel_stop` | `exit_last_progress_at` |
| Cancel stop done | log cancel outcome; stop cleared in JSON | `active_stop` null, stop_history `cancelled` |
| Spread close submitted | `Manual kill spread close working` | `spread_close_order_id`, `exit_last_step=spread_close_working` |
| Spread close filled | `spread close filled` / `_apply_spread_close_fill` | `status=closed` or closing complete |
| Exchange stop fill (C2) | `Exchange stop filled` | `short_closed_at`, `exit_handler=exchange_stop` |
| Long chase tick | `v3_exit` `"handler":"long_chase"` | `exit_last_step=long_chase_tick` |
| Moved to closed | status in JSON | `status=closed`, `close.timestamp` if set |

**Future improvement (TABLED):** Dedicated ISO fields per stage (`cancel_stop_done_at`, etc.). For now, correlate `exit_last_step` + `exit_last_progress_at` with log line timestamps.

Example — extract today's v3_exit events:

```powershell
Select-String -Path meic0dte/logs/stop_monitor.log -Pattern "v3_exit" |
  Select-Object -Last 30 | ForEach-Object { $_.Line }
```

---

## Priority order

```text
DONE  F-1  open + close_only_mode → re-enqueue ManualKillHandler
DONE  F-2  mtime-gated discovery + throttle pending_fill_sync

WEEK  Live C1/C2 observation (natural events only)
WEEK  T-1  Broker parallelism — EOD log audit + optional kill burst
WEEK  T-2  Heartbeat loop rate — EOD passive check
WEEK  T-3  Cold-cache — only if restart + exits same session
LATER T-4  Priority queue — if 6+ simultaneous kills tested
LATER T-5  Breach restart recovery — after first live breach
```

---

## Regression commands

```powershell
# F-1 recovery test
python -m pytest tests/test_v3_paper_scenarios.py::TestRestartMidClose -v

# Full V3 tests
python -m pytest tests/test_v3_*.py -v

# Full suite (254 tests)
python -m pytest tests/ -q

# Broker spike (T-1, read-only)
python scripts/v3_broker_spike.py --order-ids <ids>
```

---

## Cross-reference

Design doc §16.7 item 6 → F-1 fix  
Design doc §16.8 Monday checklist → superseded by **Active session** + **EOD passive audit** sections above for this week.

---

*Next update: after first full V3 market week — mark T-1/T-2/T-5 PASS/FAIL from EOD audit results.*
