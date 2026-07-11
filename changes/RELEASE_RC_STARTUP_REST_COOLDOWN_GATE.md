# Release Candidate — Startup REST Cooldown Gate

**Status:** RC — **do not merge or run live until operational checklist complete**  
**Date:** 2026-07-11  
**Branch:** `master` (uncommitted working tree)  
**Base commit:** `d7a4ca61b36319e13cef02f40f33f2c1362502b5`  
**Spec:** `changes/STARTUP_REST_COOLDOWN_GATE_DESIGN_FINAL.md`

---

## Merge recommendation

**HOLD** — code and automated validation are green; **do not merge to live** until:

1. Jul 10 stale `11-00_*` active JSONs are quarantined/reconciled.
2. Broker confirms no orphan working PCS order or open 7525/7500 vertical.
3. One-day off-hours dry run completed with Task Scheduler command verified.
4. Operator signs off on dashboard Re-check / Resume workflow.

After the above: merge as a single RC branch/PR, deploy **Monday pre-market only** with `NEW_RISK_GATE_ENABLED=true` and Task Scheduler `python run.py --one-day`.

---

## Full regression

```bash
pytest tests/ -q --ignore=tests/integration
```

| Result | Count |
|--------|------:|
| **Passed** | **570** |
| Failed | 0 |
| Warnings | 2 (pre-existing Windows heartbeat concurrency + asyncio mock) |

Prior baseline before gate RC: 538 passed (5 failures fixed: gate isolation + `iter_active_trade_paths` patch + manual test mocks + missing `chase` import).

---

## Focused RC test suite

```bash
pytest tests/test_trading_gate.py \
       tests/test_rest_probe.py \
       tests/test_trading_gate_semantics.py \
       tests/test_broker_rest_direct_calls.py \
       tests/test_jul10_gate_replay.py \
       tests/test_opening_duplicate_guard.py \
       tests/test_run_one_day.py \
       tests/test_entry_runner.py \
       tests/test_manual_place_dispatch.py -q
```

**47 passed** (gate, probe, semantics, direct REST, Jul 10 replay, duplicate guard, one-day, entry runner, manual 423).

---

## Jul 10 integrated replay

**Test:** `tests/test_jul10_gate_replay.py::TestJul10GateReplay::test_jul10_incident_replay_no_duplicate_openings`

| Assertion | Result |
|-----------|--------|
| Opening orders for slot | **1** |
| Blind cancellations | **0** |
| Replacement opening orders | **0** |
| Sibling workers spawned after latch | **0** |
| `entry_control=cooldown_blind` | ✓ |
| `open_order.status=visibility_unknown` | ✓ |
| Trade JSON persisted for stop-monitor | ✓ |
| `new_risk_latched` after visibility failure | ✓ |

---

## Direct REST call counts

**Test:** `tests/test_broker_rest_direct_calls.py`

| Method | Verified behavior |
|--------|-------------------|
| `probe_orders_rest()` | Exactly **one** `account.get_live_orders()` via `run_coroutine_threadsafe`; **zero** per-order `get_order()` calls |
| `get_order_status_direct()` | Exactly **one** `account.get_order(order_id)`; **zero** `get_live_orders_cached()` calls |
| Provenance | `filled_price_source` preserved on direct status path |
| Cooldown | `BrokerCooldownActive` propagates (not swallowed) |

---

## One-active-opening-order invariant

**Implementation:** `blocks/entry/opening_duplicate_guard.py`

- Append-only `entry_attempts` chain + `current_open_order_id`
- `assert_replacement_allowed()` blocks when prior attempt is working, unknown, partial, cancel-unconfirmed, or broker shows open position/working spread
- Returns `ENTRY_DUPLICATE_RISK_BLOCKED` from `meic_worker` before chase replacement
- `record_entry_attempt()` / `update_latest_attempt_status()` on place and cancel-confirm
- Broker helper: `find_working_open_spread_orders()` on TastyTrade broker

**Tests:** `tests/test_opening_duplicate_guard.py` (8 cases)

| Scenario | Covered |
|----------|---------|
| Previous working order | ✓ |
| Previous unknown order | ✓ |
| Partial fill | ✓ |
| Cancel unconfirmed | ✓ |
| Terminal cancel confirmed → allow | ✓ |
| Broker position already open | ✓ |
| Filled during cancel | ✓ (via Jul 10 replay handoff path; worker returns entered, no replacement) |
| Attempt chain persisted | ✓ |

---

## `run.py --one-day` lifecycle

**Tests:** `tests/test_run_one_day.py`

| Requirement | Test |
|-------------|------|
| Default `run.py` retains `while True` weekly loop | `test_default_main_module_has_persistent_loop` |
| `--one-day` branch exists (no weekly sleep) | `test_one_day_branch_skips_weekly_sleep` |
| Holiday/FOMC exits without streamer | `test_holiday_skips_trading` |
| Session closed before start skips streamer/entry | `test_session_closed_before_start_skips_entries` |
| Startup probe failure still starts services | `test_startup_probe_failure_still_starts_services` |
| Launcher lock prevents duplicate | `test_launcher_lock_prevents_duplicate` |
| Central Time wait helper | `test_wait_until_central_blocks_until_target` |

**Not fully automated (manual/off-hours):** 3:00 PM child stop, 3:30 PM EOD cleanup, dashboard terminate on exit — covered by code inspection of `run.py` `main()` finally block and `__main__` `one_day` branch; recommend clock-controlled dry run Sunday.

---

## Gate and dashboard semantics

**Tests:** `tests/test_trading_gate.py`, `tests/test_trading_gate_semantics.py`, `tests/test_manual_place_dispatch.py`

| Requirement | Status |
|-------------|--------|
| Startup `unknown` blocks new risk | ✓ |
| Re-check success clears cooldown, **not** latch | ✓ |
| Resume requires fresh probe | ✓ |
| Resume rejected while cooldown active | ✓ |
| Resume rejected with unresolved `cooldown_blind` | ✓ |
| Manual place HTTP 423 **before** row creation | ✓ |
| Dashboard `build_summary()` makes zero broker calls | ✓ |
| Row pauses unchanged by resume | ✓ (resume only clears latch; no CSV writes) |

---

## Complete file list

### New files

| Path |
|------|
| `common/trading_gate.py` |
| `common/rest_probe.py` |
| `common/entry_risk_lane.py` |
| `blocks/entry/opening_duplicate_guard.py` |
| `tests/test_trading_gate.py` |
| `tests/test_rest_probe.py` |
| `tests/test_trading_gate_semantics.py` |
| `tests/test_broker_rest_direct_calls.py` |
| `tests/test_jul10_gate_replay.py` |
| `tests/test_opening_duplicate_guard.py` |
| `tests/test_run_one_day.py` |
| `changes/RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md` |

### Modified files

| Path |
|------|
| `.env.example` |
| `run.py` |
| `common/broker_cooldown.py` |
| `brokers/tastytrade_broker.py` |
| `blocks/entry/runner.py` |
| `blocks/entry/meic_worker.py` |
| `blocks/entry/manual_worker.py` |
| `blocks/session/manual_helpers.py` |
| `meic0dte/open/open_spread_tt.py` |
| `dashboard/server.py` |
| `dashboard/templates/index.html` |
| `tests/test_entry_runner.py` |
| `tests/test_fill_provenance_phase1.py` |
| `tests/test_manual_entry_claim.py` |
| `tests/test_manual_place_dispatch.py` |
| `tests/test_manual_session.py` |

---

## Known limitations

1. **Cross-process REST token bucket** — not implemented; gate + stagger reduce burst risk but do not cap account-wide REST volume.
2. **`has_unresolved_visibility_unknown()`** scans live `trades/active/` — Jul 10 stale JSONs on disk will block Resume until quarantined.
3. **`run.py --one-day` EOD/dashboard terminate** — logic present but not clock-simulated in CI; requires off-hours dry run.
4. **Manual worker chase** — uses direct status path but does not yet record full `entry_attempts` chain (MEIC path is primary).
5. **Windows heartbeat concurrency test** — intermittent `PermissionError` under parallel pytest (pre-existing; passed in final full run).
6. **Schwab broker** — gate/probe/direct-status methods are TastyTrade-specific; Schwab path unchanged.

---

## Operational validation (before Monday)

### A. Quarantine Jul 10 artifacts

```powershell
# Inspect
Get-ChildItem trades\active\MEIC_IC\11-00_*_20260710*.json

# Move to quarantine (do not delete until broker reconciled)
New-Item -ItemType Directory -Force runtime\quarantine\2026-07-10
Move-Item trades\active\MEIC_IC\11-00_*_20260710*.json runtime\quarantine\2026-07-10\
```

### B. Broker reconciliation

- Confirm no working opening order on 7525/7500 PCS.
- Confirm position qty matches operator expectation (manual management of 3× fill).
- Clear `runtime/broker_cooldown.json` if stale.

### C. One-day off-hours dry run

```powershell
cd MEIC-with-Dash-main-V2
$env:NEW_RISK_GATE_ENABLED='true'
python run.py --one-day --force --no-stop-monitor
```

Verify: startup probe runs, gate file created, dashboard banner if latched, process exits after session-end path.

### D. Task Scheduler (recommended)

| Setting | Value |
|---------|-------|
| Program | `C:\path\to\venv\Scripts\python.exe` |
| Arguments | `run.py --one-day` |
| Start in | `...\MEIC-with-Dash-main-V2` |
| Trigger | Mon–Fri 8:00 AM CT |
| If already running | **Do not start a new instance** |
| Restart on failure | 5 min, max 3 |

---

## Sign-off checklist

- [x] Full regression 570/570
- [x] Direct REST call-count tests
- [x] Jul 10 replay test
- [x] One-active-order guard implemented + tested
- [x] Gate/dashboard semantics tests
- [x] `run.py --one-day` unit coverage
- [ ] Jul 10 active JSON quarantine (operator)
- [ ] Broker orphan/position reconciliation (operator)
- [ ] Off-hours one-day dry run (operator)
- [ ] Task Scheduler venv path verified (operator)

**RC verdict:** Ready for staged merge after operator checklist. **Not approved for live Monday until checklist complete.**
