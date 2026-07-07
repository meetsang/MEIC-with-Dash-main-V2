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
| V3 live validation | Pending | 2026-07-06 | Morning checklist above |
| Broker hardening live smoke | Pending | 2026-07-06 | IP blocked; mocks pass |
| F-7 MQTT gate polish | Deferred | 2026-07-06 | P1 |
| F-11 naming cleanup | Deferred | 2026-07-06 | P2 |
| R-1 orphan lock automation | Deferred | 2026-07-06 | Manual check script adopted |

### Live sign-off

| Date | Operator | V3 validated | Broker smoke | Notes |
|------|----------|--------------|--------------|-------|
| 2026-07-07 | | | | |
