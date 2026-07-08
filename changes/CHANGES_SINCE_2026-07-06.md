# Changes Since July 6, 2026

**Project:** MEIC-with-Dash-main-V2  
**Started:** 2026-07-06  
**Purpose:** Living changelog — every implementation, finding, and operator decision **from July 6 onward**.

Older history (V2 rewrite, pre-Jul-6 design) stays in the other `changes/` docs. **Add a new dated section at the top** whenever you ship fixes, discover issues, or make operator decisions.

---

## How to maintain this doc

After any meaningful change (code merge, live session finding, incident, config decision):

1. **Add a new `## YYYY-MM-DD` section above the previous entry** (newest first).
2. Include:
   - **What changed** — files, behavior, env vars
   - **Why** — incident, review finding, or goal
   - **Findings / learnings** — anything useful going forward
   - **Tests / validation** — pytest count, live smoke, checklist items
   - **Deferred** — what was explicitly not done
3. Link to deeper specs in `changes/` when they exist (incident docs, design docs).
4. Update the **Open items** table at the bottom if status changed.

### Entry template (copy for next change)

```markdown
## YYYY-MM-DD — Short title

**Status:** implemented | in progress | deferred | live-validated  
**Tests:** `uv run pytest tests/ -q` → N passed

### What changed
- ...

### Findings / learnings
- ...

### Validation
- [ ] ...

### Related
- [link](path)
```

---

## 2026-07-07 (EOD) — Jul 7 live session: F-9 close, shared-stop P0, EOD settlement, breach review

**Status:** implemented (code); live-validated partial; new day = fresh JSONs (no repair needed on prior shared-stop state)  
**Tests:** `uv run pytest tests/test_shared_stop_per_tranche.py tests/test_inspect_spread_position.py tests/test_expiry_settlement.py tests/test_v3_*.py -q` → **49+ passed** (session-related suites)

**Operator log:** [LIVE_SESSION_2026-07-07.md](LIVE_SESSION_2026-07-07.md)  
**Shared-stop spec:** [SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md](SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md)

---

### Session timeline (CT)

| Time | Event | Outcome |
|------|-------|---------|
| 08:43–09:15 | **ms-184** manual put 7445/7400 — open, stop, exchange-stop exit | ✓ V3 path OK; did not exercise manual-close preflight |
| 09:16 | **ms-185** manual put 7425/7400 qty 6 — open + stop | ✓ |
| 11:04–11:07 | Streamer stale on ms-185 | Breach checks frozen (F-3); exchange stop still live |
| 11:17 | Dashboard **Close** on ms-185 | Stop cancelled; spread close **blocked** — `preflight_mismatch` |
| ~11:25 | **F-9 fix** shipped (`_signed_position_qty`) | Code fix; ms-185 already flat manually @ 11:30 |
| ~13:16 | **ms-186** manual dashboard close | ✓ Validated after F-9 restart |
| 11:59–13:44 | MEIC 7485P tranches (12-00_P, 12-30_P, 01-45_P) | **Shared stop** `481561791` reconciled into multiple JSONs — only 1 broker stop |
| ~13:49 | Software breach wave on 7485P | 12-30_P, 01-45_P closed via `software_breach`; large slippage; cross-tranche stop fights |
| 15:00+ | **02-00_C** EOD PnL wrong | Stale `trades/settlement/2026-07-07.json` SPX **7524.29** → showed −$374 vs +$55 at SPX ~7503 |
| EOD | MQTT settlement capture + source priority fix | SPX **7503.205** captured; 02-00_C → +$55 |
| Evening | **Shared stop per tranche (P0)** implemented | Production reconcile no longer adopts by symbol; repair CLI only |

---

### 1. Incident — ms-185 manual close blocked (F-9 preflight)

**Symptom:** Dashboard Close cancelled exchange stop `481416212` but never placed spread close (~450+ recovery polls, `exit_error=preflight_mismatch`).

**Root cause:** Tasty positions use **unsigned** `quantity` + `quantity_direction` (`Short` / `Long`). `inspect_spread_position()` expected signed qty (short &lt; 0) → `mismatch` on valid 6-lot vertical.

**Fix**

| File | Change |
|------|--------|
| `brokers/tastytrade_broker.py` | `_signed_position_qty()` — Short → negative, Long → positive before F-9 closable check |
| `tests/test_inspect_spread_position.py` | ms-185 7425/7400 scenario (8 tests) |

**Operator impact:** Naked short exposure after stop cancel until manual flat @ Tasty (~11:30, +$0.65/sp vs $0.70 entry).

**Validation:** ms-186 manual close ~13:16 after monitor restart — pass.

**Deferred:** Recovery backoff when `exit_error=preflight_mismatch` repeats (position API storm).

---

### 2. Incident — MEIC shared stops on same strike (7485P / 7535C)

**Symptom:** Second and third tranches on **7485P** (and calls on **7535C**) did not get their own exchange stops. JSON for 12-30_P, 01-45_P pointed at **12-00_P**’s stop `481561791`.

**Root cause**

| Mechanism | Location |
|-----------|----------|
| Slow sync calls `find_working_close_order(short_sym)` and **adopts** first BTC | `StopMonitor._reconcile_active_stop_with_broker()` |
| `stop_is_current()` true per JSON after adopt → `setup_initial_stop()` skipped | `fill_sync.py`, `_ensure_stop_for_filled_qty()` |

**Afternoon impact (~13:49 CT)**

- Software breach **did** close unprotected puts (resilience worked).
- Breach cancelled the **one** shared stop; tranches **competed** for limits (cancel 422 retries, reprice chase).
- **12-00_P** briefly had **no** exchange stop until replaced ~13:49:41.
- Operator slippage large (−$95 to −$105/lot vs 2× credit policy) — execution gap (short mid chase), not PnL math bug.

**Investigation — breach timing (deferred, no code change)**

| Lot | Breach threshold | Spread at fire | Breach → short fill | Breach → spread flat |
|-----|------------------|----------------|---------------------|----------------------|
| 01-45_P | $0.80 (2× $0.30 + $0.20) | $0.83 @ 13:49:21 | ~14s | ~46s (30s long-leg delay dominates) |
| 12-30_P | $1.10 (2× $0.45 + $0.20) | $1.30 @ 13:49:25 | ~8s | ~40s |

Threshold uses **`two_x_net_credit + $0.20`**, not raw 2× spread — fires later than operator “spread at 2×” mental model. Tightening deferred.

---

### 3. Fix — Shared stop per tranche (P0 live-safety, evening)

**Operator decisions (recorded in plain-English doc):**

- **Stop placement:** always one broker stop per tranche JSON — never adopt another tranche’s BTC during normal sync/fill.
- **Close / cancel:** within-trade only — breach/manual cancel **this JSON’s** `active_stop.order_id` only.
- **Repair:** explicit CLI only (`sync-broker-stop` / `repair-orphaned-stops`), dry-run default, strict qty/price/unclaimed matching; refuse ambiguous matches.

**What changed**

| Area | Files | Behavior |
|------|-------|----------|
| Per-tranche reconcile | `blocks/stop/monitor.py` | `_reconcile_active_stop_with_broker()` refreshes **own** `order_id` only; no `find_working_close_order` |
| Stop place | `monitor.py` `_ensure_stop_for_filled_qty()` | Places new stop even if another BTC exists on symbol |
| Ownership guard | `blocks/stop/stop_ownership.py` | V3 cycle scans duplicate `active_stop.order_id`; `CRITICAL` + `lifecycle.stop_ownership_conflict`; blocks auto-place until repair |
| `stop_is_current` | `blocks/stop/fill_sync.py` | `ownership_conflict=True` → not current |
| V3 supervisor | `blocks/stop/v3/supervisor.py` | Ownership scan each cycle; conflict-aware stop gate |
| Repair-only adopt | `blocks/stop/broker_sync.py` | `repair_orphan_stop()` — strict match; `adopt_active_stop_from_broker()` → repair wrapper |
| CLI | `tests/adhoc_integration.py`, `scripts/repair_orphaned_stops.py` | `sync-broker-stop` / `repair-orphaned-stops` — **`--apply`** to write JSON |
| Removed from production | `monitor.py` | Dead `_cancel_all_close_orders_on_short` path; `cancel_all_close_orders_on_short` = repair CLI only |

**Tests:** `tests/test_shared_stop_per_tranche.py` (12), updated `tests/test_broker_sync.py`, `tests/test_stop_fill_long_close.py`

**Next session:** Fresh JSONs — no need to repair Jul 7 shared-stop state. Restart stop monitor after deploy.

```powershell
# If ever needed on stale JSONs (dry-run first):
uv run python tests/adhoc_integration.py sync-broker-stop
uv run python tests/adhoc_integration.py sync-broker-stop --apply
```

---

### 4. Fix — EOD settlement SPX source priority + MQTT capture

**Symptom:** **02-00_C** (7520/7545 calls) showed **−$374** at EOD; SPX ~7503 → should be **+$55** (OTM, expire $0).

**Root cause:** `trades/settlement/2026-07-07.json` had stale manual **`spx_close: 7524.29`** (above 7520 short strike). Settlement path preferred that file over polls/MQTT.

**Fix**

| File | Change |
|------|--------|
| `common/expiry_settlement.py` | Priority after 15:00 CT: locked manual → **mqtt_settlement** → OHLC → polls; ignore unlocked stale manual if &gt;5pt disagree |
| `common/expiry_settlement.py` | `capture_mqtt_settlement_close()` → `data/YYYY-MM-DD/spx_mqtt_settlement.json` |
| `blocks/stop/v3/supervisor.py` | `_maybe_capture_mqtt_settlement()` each cycle after 3 PM |
| `dashboard/server.py` | `/api/history/sync` with `spx_close` writes `locked: true` operator settlement |

**Result:** Captured MQTT **7503.205** → 02-00_C **+$55**.

**Tests:** `tests/test_expiry_settlement.py` (10 passed)

---

### 5. Deferred / tabled (no code this session)

| Topic | Status | Notes |
|-------|--------|-------|
| Software breach threshold | **Deferred** | Change `two_x_net_credit + $0.20` → raw 2× spread? |
| Breach execution price | **Deferred** | Cap limit to ~2× spread debit vs short mid chase |
| Recovery backoff on `preflight_mismatch` | **Deferred** | ms-185 API storm |
| `TESTING.md` doc drift | **Deferred** | Still says `two_x_short + 0.20` |

---

### Jul 7 validation sign-off

| Check | Result | Notes |
|-------|--------|-------|
| Manual spread open + stop | **pass** | ms-184, ms-185, ms-186 |
| Manual dashboard close | **pass** (after F-9) | ms-185 fail → fix; ms-186 ~13:16 |
| Exchange stop exit | **pass** | ms-184 |
| MEIC per-tranche stops | **fail → fixed evening** | Shared-stop P0; fresh JSONs next day |
| Software breach resilience | **pass** | Closed puts when stops missing; slippage high |
| Software breach policy | **review** | Threshold offset + execution chase |
| EOD settlement PnL | **pass** (after fix) | MQTT capture + priority |
| Streamer / stop monitor | **partial** | Stale episodes; breach wave ~13:49 |

---

### Files touched (Jul 7 — all code changes)

```
brokers/tastytrade_broker.py          # F-9 _signed_position_qty
common/expiry_settlement.py           # EOD settlement priority + MQTT capture
blocks/stop/monitor.py                # Per-tranche reconcile; ownership quarantine
blocks/stop/broker_sync.py            # repair_orphan_stop; no production adopt
blocks/stop/stop_ownership.py         # NEW — duplicate order_id guard
blocks/stop/fill_sync.py              # ownership_conflict on stop_is_current
blocks/stop/v3/supervisor.py          # Ownership scan; MQTT settlement capture
dashboard/server.py                   # Locked operator settlement on history sync
tests/test_inspect_spread_position.py
tests/test_expiry_settlement.py
tests/test_shared_stop_per_tranche.py # NEW
tests/test_broker_sync.py
tests/test_stop_fill_long_close.py
tests/adhoc_integration.py            # sync-broker-stop --apply
scripts/repair_orphaned_stops.py      # NEW
changes/LIVE_SESSION_2026-07-07.md
changes/SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md
```

---

## 2026-07-07 — F-9 preflight fix (Tasty qty-direction) + ms-185 close failure

**Status:** superseded by [EOD summary above](#2026-07-07-eod--jul-7-live-session-f-9-close-shared-stop-p0-eod-settlement-breach-review) — kept for grep/history

**Tests:** `uv run pytest tests/test_inspect_spread_position.py -q` → 8 passed

### What changed

- `brokers/tastytrade_broker.py` — `_signed_position_qty()` normalizes Tasty `quantity` + `quantity_direction` to signed contracts before `inspect_spread_position()` closable check
- `tests/test_inspect_spread_position.py` — ms-185 case (7425 short / 7400 long, qty 6)

### Why

ms-185 manual close @ 11:17 CT: stop cancelled, spread close blocked with `preflight_mismatch`. Tasty reports short leg as `quantity=6, quantity-direction=Short` but F-9 expected `short_qty < 0`.

### Related

- [LIVE_SESSION_2026-07-07.md](LIVE_SESSION_2026-07-07.md)

---

## 2026-07-07 (earlier) — V3 live validation: open/stop OK; manual close blocked (F-9)

**Status:** superseded by [EOD summary](#2026-07-07-eod--jul-7-live-session-f-9-close-shared-stop-p0-eod-settlement-breach-review)

### What validated (vs Jul 6 incident)

- [x] Entry → fill → stop placement (ms-184, ms-185)
- [x] Breach arm / watch without false software exit on open
- [x] ms-184 exited cleanly via **exchange stop** (did not hit manual-close preflight)
- [ ] Manual dashboard close — **failed** ms-185 @ 11:17 CT (fixed same day)

---

## 2026-07-06 (late) — Manual spread dashboard dedupe + operator cleanup

**Status:** implemented (code); operator trade files cleaned locally  
**Tests:** `uv run pytest tests/test_build_manual_trades.py -q` → pass

### What changed

| Item | Summary |
|------|---------|
| **Dashboard dedupe** | `load_dashboard_manual_trades()` keys by `lot_side`, keeps newest archive — not by filename |
| **Regression test** | `test_duplicate_history_archives_dedupe_by_lot_side` in `tests/test_build_manual_trades.py` |

### Findings / learnings

- **Why duplicates appeared:** Re-seeded `ms-99`/`ms-100` test fixtures + V3 close/finalize each wrote a **new** JSON under `trades/history/MANUAL_SPREAD/` (different timestamps in filename). Dashboard merged all “closed today” history files but deduped only by **filename** → one row per archive (9 files = 9 rows).
- **MEIC hanging:** Jul 6 `11-00` legs were `status: closed` but still in `trades/active/MEIC_IC/`; session CSV still `entered`. Grid showed them as live until active files removed.
- **Ctrl+C:** Stale `runtime/locks/` for dead PIDs (stop_monitor, streamer, market_data) — clear with `release_lock` or delete lock file if PID dead. `check_stop_monitor.py` → 0 processes.

### Operator cleanup (local, not in git)

- Removed `ms-176` active (cancelled on Tasty), `11-00` MEIC from active
- Removed duplicate `ms-99`/`ms-100` history archives from tonight’s test runs
- Session CSV: `ms-176_P` → `cancelled`; `11-00_*` → `closed` with history paths

### Files touched (code)

- `manual_spread/entry.py`
- `tests/test_build_manual_trades.py`

---

## 2026-07-06 (evening) — ChatGPT pre-live follow-up review

**Status:** implemented  
**Tests:** `uv run pytest tests/ -q` → **282 passed**

### What changed

| Item | Summary |
|------|---------|
| **Unknown preflight** | `place_spread_close_order()` blocks `unknown` position state; only `allow_unverified_emergency_close=True` bypasses (operator emergency only) |
| **AlertListener session** | `blocks/stop/run.py` reuses `get_shared_broker()` session/account — no second OAuth session |
| **Start bot lock** | `/api/start_bot` checks `runtime/locks/launcher.lock` via `active_lock_pid()` → 409 `already_running_external_lock` |
| **Phase 3 gating** | Phase 3 SPX proximity evaluated before option-leg breach arm; not blocked by `breach_watch` `no_prices` |
| **Tests** | 8 new tests across `test_v3_incident_fixes.py`, `test_broker_hardening.py` |

### Findings / learnings

- `unknown` position from failed REST preflight must **block** spread close — same hazard class as `flat` (accidental open).
- Emergency override is explicit opt-in only — not for software-breach or duplicate recovery paths.
- Phase 1 breach still requires option-leg MQTT; Phase 3 only needs SPX + time + working stop.

### Files touched

- `brokers/tastytrade_broker.py`, `brokers/base.py`
- `blocks/stop/v3/recovery.py`, `blocks/stop/v3/supervisor.py`, `blocks/stop/run.py`
- `common/process_lock.py` (`active_lock_pid`)
- `dashboard/server.py`

---

## 2026-07-06 — V3 false-breach incident repair + broker hardening

**Status:** implemented (live validation pending)  
**Tests:** `uv run pytest tests/ -q` → **274 passed**

### Issues found (live session)

| Time (CT) | Issue | Impact |
|-----------|-------|--------|
| Pre-open | Weekend test fixtures `ms-99`, `ms-100` left in `trades/active/` | Cluttered active-trade scan; removed before open |
| 09:25 | Dashboard broker spam on 2–3s poll | Fresh OAuth session every poll; partial fix same day |
| 10:59 | **V3 false breach on 11-00 IC** | Immediate close → duplicate close on flat account → accidental debit spreads at Tasty |

**Root cause (11-00 incident):** V3 treated `phase.should_activate()` (true for any open trade) as “start software breach exit,” set `close_only_mode`, recovery routed `breach_*` to `ManualKillHandler`, duplicate-close guards failed.

**Mitigation during fix window:** `STOP_MONITOR_ENGINE=v2`

### What changed — V3 incident repair

| ID | Summary | Primary files |
|----|---------|---------------|
| F-3 | `PhaseAction` enum + `evaluate()` — separate watch vs exit | `blocks/stop/phases.py` |
| F-4 | `resolve_exit_recovery_route()` — breach recovery ≠ manual kill | `blocks/stop/v3/recovery.py`, `supervisor.py` |
| F-8 | Lifecycle gates: stop → MQTT → breach armed before breach eval | `blocks/stop/v3/supervisor.py` |
| F-5 | Duplicate-close + flat-account guards in `ManualKillHandler` | `blocks/stop/v3/handlers/manual_kill.py` |
| F-6 | `finalize_v3_exit_state()` after successful close | `blocks/stop/v3/handlers/software_breach.py` |
| F-9 | Broker preflight in `place_spread_close_order` | `brokers/tastytrade_broker.py` |
| F-10 | `display_close_prices()` for dashboard PnL dedup | `blocks/stop/close_fills.py`, `dashboard/server.py` |
| R-9 | `short_closed_at` from broker `filled_at` (30s long-leg from fill time) | `blocks/stop/monitor.py` |

**Deferred:** F-7 (MQTT gate polish), F-11 (naming cleanup), R-1 (automatic orphan cleanup at EOD)

### What changed — broker / IP hardening

| Component | Location |
|-----------|----------|
| `get_shared_broker()` — one session per process | `common/broker_factory.py` |
| REST limiter (1 req/s, burst 3) | `common/rest_limiter.py` |
| Cooldown circuit breaker (skip LOW/NORMAL 5 min) | `common/broker_cooldown.py` |
| Live orders cache (TTL 2s) | `brokers/tastytrade_broker.py` |
| Dashboard fill sync gate + lock | `dashboard/broker_fill_sync.py` |
| Process locks (`runtime/locks/`) | `common/process_lock.py` — wired in `run.py`, `blocks/stop/run.py`, streamer, market_data, dashboard |
| `GET /api/broker_health` | `dashboard/server.py` |
| Start bot 409 when launcher active | `dashboard/server.py` |
| `MEIC_ALLOW_LIVE_BROKER_TESTS` pytest gate | `common/broker_factory.py` |
| `STOP_BROKER_LANE_SIZE` default **6 → 1** | `blocks/stop/v3/config.py` |
| Shared broker callers | `run.py`, `blocks/stop/run.py`, dashboard fill sync, manual spread, GEX |

**New tests:** `tests/test_v3_incident_fixes.py`, `tests/test_broker_hardening.py`

### What changed — operator tooling

- `scripts/check_stop_monitor.py` — Python version (PowerShell `.ps1` blocked by execution policy on operator machine)
- `V2_README.md` — pre-open / EOD stop-monitor check docs

### Findings / learnings (carry forward)

**V3 control flow**
- `should_activate()` ≠ breach — it means “this phase may monitor,” not “close now”
- Scan thread decides; confirmed exits on worker threads — never start exit I/O from a misclassified scan
- Lifecycle gates mandatory before breach eval (F-8)
- Recovery must use explicit route table — F-1 “resume manual kill if `close_only_mode`” is unsafe for `breach_*`
- Duplicate close on flat account can **open** positions at Tasty — F-5/F-9 are P0
- Phase 3 had same wiring risk as Phase 1 until F-3

**Broker / infrastructure**
- Multiple `get_broker()` calls = multiple OAuth sessions = rate-limit risk → use `get_shared_broker()`
- HIGH priority for orders/cancels; LOW/NORMAL skipped during cooldown
- Orphan `blocks/stop/run.py` duplicates traffic — locks + manual check script
- F-8 stop gate uses slow path (~10s) cache, not per-scan REST (R-6)
- Long-leg chase: 30s from broker fill time, not detection time (R-9)

**Operator workflow**
- Production entry: `uv run python run.py` only (Mosquitto separate)
- Rollback: `STOP_MONITOR_ENGINE=v2` in `.env` + restart

### Operator decisions

| Topic | Decision |
|-------|----------|
| V3 go-live | P0 + F-10 tonight; V3 live after morning validation |
| F-3 | Full `PhaseAction` enum |
| F-10 | Include before live |
| R-1 orphans | Defer automation; use `check_stop_monitor.py` manually |
| IP hardening | Ship alongside V3 (independent failure domains) |

### Env knobs added / changed

```env
TT_REST_MAX_PER_SEC=1
TT_REST_BURST=3
TT_BROKER_COOLDOWN_SEC=300
TT_LIVE_ORDERS_CACHE_TTL_SEC=2
STOP_BROKER_LANE_SIZE=1
STOP_MONITOR_ENGINE=v3    # after morning validation
```

### Validation checklist (next session)

```powershell
uv run python scripts/check_stop_monitor.py   # 0 before start
uv run pytest tests/ -q                       # all green
uv run python run.py                          # single launcher
# After start: 1 stop-monitor; no immediate breach on new entry
# http://localhost:5002/api/broker_health — no cooldown, launcher lock present
```

**Red flags:** immediate close after entry; `Resuming manual kill ... breach_phase1_initial_stop`; duplicate closes; 401/429 spam; >1 stop-monitor PID.

### Deep-dive docs (same incident)

| Doc | Use when |
|-----|----------|
| [LIVE_SESSION_2026-07-06.md](LIVE_SESSION_2026-07-06.md) | Raw operator log |
| [STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md](STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md) | Full RCA + patch specs |
| [STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md](STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md) | Plain-English Q&A |

### Prior context (before Jul 6 — not duplicated here)

| Doc | Relevance |
|-----|-----------|
| [LIVE_SESSION_2026-07-02.md](LIVE_SESSION_2026-07-02.md) | Broker serializes on one event loop |
| [STOP_MONITOR_V3_DESIGN.md](STOP_MONITOR_V3_DESIGN.md) | V3 architecture |
| [STOP_MONITOR_V3_REVIEW_FIXES.md](STOP_MONITOR_V3_REVIEW_FIXES.md) | Pre-incident review |
| [V2_MODULAR_REWRITE.md](V2_MODULAR_REWRITE.md) | V2 rewrite history |

---

## Open items (rolling)

| Item | Status | Since | Notes |
|------|--------|-------|-------|
| V3 live validation | **Partial pass** | 2026-07-06 | Jul 7: open/stop/close OK after F-9; MEIC shared-stop fixed evening |
| F-9 preflight qty sign (Tasty) | **Fixed** | 2026-07-07 | ms-186 validated ~13:16 |
| Shared stop per tranche (P0) | **Fixed** | 2026-07-07 | Evening deploy; fresh JSONs next session |
| EOD settlement MQTT capture | **Fixed** | 2026-07-07 | Priority + `spx_mqtt_settlement.json` |
| Software breach threshold (2× vs 2×+$0.20) | **Deferred** | 2026-07-07 | Afternoon breach review |
| Software breach execution (spread cap) | **Deferred** | 2026-07-07 | Large slippage on Jul 7 breach exits |
| Recovery backoff on preflight_mismatch | **Deferred** | 2026-07-07 | ms-185 API storm |
| Broker hardening live smoke | **Partial** | 2026-07-06 | Scanner + place worked |
| F-7 MQTT gate polish | Deferred | 2026-07-06 | P1 |
| F-11 naming cleanup | Deferred | 2026-07-06 | P2 |
| R-1 orphan lock automation | Deferred | 2026-07-06 | Manual `check_stop_monitor.py` |
| TESTING.md breach threshold doc | Deferred | 2026-07-07 | Says `two_x_short`; code uses `two_x_net_credit` |

### Live sign-off

| Date | Operator | V3 validated | Broker smoke | Notes |
|------|----------|--------------|--------------|-------|
| 2026-07-07 | | **partial → pass** (post-fix) | partial | F-9, shared-stop P0, EOD settle; breach slippage tabled |
| 2026-07-06 | | blocked | partial | False breach incident; V3 repair shipped |
