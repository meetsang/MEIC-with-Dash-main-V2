# Pre-Entry REST Probe Hardening — Design Spec

**Status:** IMPLEMENTED on `fix/pre-entry-rest-probe-hardening` (do **not** merge to `master` until RC sign-off)  
**Date:** 2026-07-13 (amended same evening; implementation started)  
**Incident:** [LIVE_SESSION_2026-07-13.md](LIVE_SESSION_2026-07-13.md) — **11-00 MEIC never spawned**  
**Related:** [RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md](RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md), `common/trading_gate.py`, `common/rest_probe.py`, `blocks/entry/runner.py`  
**Priority:** P0 before next live week

---

## 1. Problem statement

On **2026-07-13**, the live launcher was healthy through the morning (streamer + stop_monitor live), the session CSV had `11-00_P/C` as `pending` / unpaused with window `10:59–11:05`, and the market was open — yet **no entry workers spawned**, no trade JSON was written, and the window was lost forever.

The new-risk REST gate (shipped Jul 10–11) requires a fresh REST probe before opening risk. That probe currently runs **synchronously on the launcher main loop** via **`get_broker()`** (brand-new OAuth + broker). Evidence shows the probe **never completed** — `trading_gate.json` still had only the **08:00:15 startup** probe when 11:00 arrived. The main loop never logged `Spawned entry worker`, and there was **no miss detection**.

A separate Windows/Avast `SSLKEYLOGFILE` OPENSSL crash can contribute (see §3.3) and already has a code + ops fix. This document covers the **entry-gate / probe coordination** path.

---

## 2. Intended automatic REST probing policy (normative)

Strict budget — **no all-day keep-warm**:

| Automatic probe | When | Count |
|-----------------|------|------:|
| Startup | Once each morning (async) | 1 |
| Pre-tranche | Once per eligible scheduled MEIC tranche, shortly before window | ≤ 6 |

**Normal maximum for six MEIC tranches = 7 automatic probes per trading day** (`1 + 6`).

Rules:

1. **One automatic startup probe** each morning.
2. **One automatic pre-tranche probe** for each scheduled MEIC tranche.
3. **One probe is shared by both put and call** rows of that tranche (`11-00_P` and `11-00_C` share the `11-00` probe).
4. **No recurring keep-warm** (`REST_KEEP_WARM_*` is rejected — do not add).
5. **No automatic retry loop** after a failed or timed-out tranche probe.
6. Manual / operator probes (dashboard Re-check, Resume path) are **separate** and do **not** authorize recurring automatic retries.

Dedup key:

```text
(session_date_ct, strategy, tranche_time)
```

where `tranche_time` is the lot label (`11-00`, `12-00`, …), **not** the P/C row.

---

## 3. Background

### 3.1 Current (broken) spawn path

```text
run.py main loop (every 5s)
  └─ EntryMonitorRunner.tick(now)
       └─ _gate_allows_spawn()
            └─ evaluate_new_risk_gate(require_fresh_probe=True)
                 └─ run_rest_probe(get_broker(), source='pre_entry')  # BLOCKING
```

`get_broker()` creates a fresh `TastyTradeBroker` in the launcher process. Any hang/OPENSSL abort blocks the main loop for the entire entry window.

### 3.2 Jul 13 evidence

| Check | Result |
|-------|--------|
| `11-00_P/C` | `pending`, unpaused, window `10:59–11:05` |
| Spawn / trade JSON | None |
| Latch / cooldown file | Not latched; no cooldown |
| Last successful probe | **08:00:15 startup only** |
| Streamer / stop_monitor | Live during window |
| market_data | Exited 10:20 with no launcher restart log (canary) |

### 3.3 Avast OPENSSL (complementary — already mitigated)

Avast injects `SSLKEYLOGFILE=\\.\aswMonFltProxy\...` → `OPENSSL_Uplink: no OPENSSL_Applink`.  
Fixed via `common/win_ssl_env.py` + entry-script sanitize + Avast File/Folder exception. **Must remain.**

---

## 4. Goals / non-goals

### Goals

1. Non-blocking launcher-owned **background probe coordinator**.
2. Startup probe **async** — streamer / market_data / stop_monitor start **without waiting** for broker create or REST.
3. Exactly one background pre-tranche probe at `PRE_TRANCHE_PROBE_LEAD_SEC` before each eligible window.
4. Main loop and `EntryMonitorRunner.tick()` never create a broker, wait for broker init, run REST sync, wait on the probe lock, or wait for network.
5. Tranche P+C share one probe result; failed/pending/success semantics as specified in §6.
6. Persist probe identity and state (§7).
7. Preserve Jul 10 latch / Resume / cooldown / `cooldown_blind` behavior.
8. Exactly-once `TRANCHE_MISSED`; main-loop stall watchdog; no automatic catch-up after window end.

### Non-goals

- Recurring keep-warm / `REST_KEEP_WARM_INTERVAL_SEC`.
- Automatic retry of a failed tranche probe.
- Auto-extend or catch-up after `entry_window_end`.
- Redesigning session CSV schema.
- Changing stop_monitor process broker lifecycle.
- Merging to `master` in this workstream (feature branch only until RC).

---

## 5. Architecture

### 5.1 Background probe coordinator (launcher-owned)

New module (suggested): `common/probe_coordinator.py` (or `blocks/entry/probe_coordinator.py`).

Owns:

- One **shared launcher broker** (`get_shared_broker()`), created only on the coordinator thread.
- Lifecycle: start with session, stop at session end.
- Queue / schedule of probe jobs: `startup` + one `pre_tranche` per tranche key.
- Persistence updates into `runtime/trading_gate.json` (and/or `runtime/rest_probes.json` if clearer — see §7).

**Main loop never touches broker REST.** It only reads probe / gate state (non-blocking).

### 5.2 Startup probe (async)

| Setting | Default |
|---------|---------|
| `REST_PROBE_ON_SESSION_START` | `true` (existing) |

Behavior:

1. At session start, coordinator schedules `source=startup` **asynchronously**.
2. `run.py` proceeds to start streamer, market_data, stop_monitor **immediately** — **no wait** for broker create or probe completion.
3. Startup failure is recorded; may latch on blocking statuses (existing probe recording rules). Does **not** stall boot.

### 5.3 Pre-tranche probe schedule

| Setting | Default | Meaning |
|---------|---------|---------|
| `PRE_TRANCHE_PROBE_LEAD_SEC` | `30` | Request probe this many seconds before `entry_window_start` |

Example: window start `10:59` → request probe ≈ `10:58:30`.

Eligibility:

- MEIC strategy rows from today’s session plan (or `MEIC_TRANCHE_SLOTS` + CSV pause/skip).
- Skip tranche if **both** P and C are paused or skip (optional: skip if no pending sides).
- Deduplicate by `(session_date, strategy, tranche_time)` so P and C produce **one** job.

Coordinator ticks (its own sleep, ~1s): when `now >= window_start − lead` and that tranche has `performed=false` and was never requested, enqueue **exactly one** probe. After start/finish, mark performed for that key — **no second attempt**.

### 5.4 Main loop / runner constraints (hard)

`run.py` main loop and `EntryMonitorRunner.tick()` **must never**:

- call `get_broker()` / `get_shared_broker()`;
- wait for broker initialization;
- call `run_rest_probe(...)` synchronously;
- acquire/wait on the probe lock;
- block on network / `future.result`.

Gate evaluation is a **pure read** of persisted probe + latch/cooldown state.

---

## 6. Gate behavior at tranche window

`evaluate_new_risk_gate` (or a thin wrapper used by the runner) for MEIC spawn must accept tranche identity, e.g.:

```text
evaluate_new_risk_gate(
  require_fresh_probe=True,
  strategy='MEIC_IC',
  tranche_id='11-00',   # lot / tranche_time
)
```

Order of checks (preserve Jul 10 semantics first):

1. Gate disabled → allow.
2. Cooldown active / `new_risk_latched` / rest status blocking → **blocked** (unchanged reasons).
3. REST readiness for this tranche:

| Probe state for `(date, strategy, tranche_id)` | Gate REST readiness |
|-----------------------------------------------|---------------------|
| Succeeded (`ok=true`, `performed=true`), valid for this window | **Pass** (both P and C) |
| Still running / scheduled / not completed | **Blocked** `rest_probe_pending` |
| Failed or timed out (`performed=true`, `ok=false`) | **Blocked**; failure recorded; latch/cooldown rules from existing probe recorder apply; **do not** start another automatic probe |
| Never scheduled (e.g. past start with no job — abnormal) | **Blocked** `rest_probe_missing` (no auto-fire on tick) |

**Validity of a successful pre_tranche probe:** remains valid for spawning that tranche’s sides until `entry_window_end` (inclusive). Do **not** require the global 60s `REST_READY_MAX_AGE_SEC` clock to authorize mid-window P/C stagger — otherwise a probe at `T−30s` would go “stale” before a staggered second side. Startup success alone does **not** authorize tranche entry.

**Critical:** repeated 5s ticks must **not** create additional probes. Runner only reads state.

Manual dashboard probe / Resume remains separate (`source=dashboard` / operator clear) and does not invent an automatic retry loop for the tranche key.

---

## 7. Persist probe identity and state

Each automatic probe record must include at least:

| Field | Example |
|-------|---------|
| `source` | `startup` \| `pre_tranche` |
| `tranche_id` | `11-00` or `null` for startup |
| `strategy` | `MEIC_IC` |
| `session_date_ct` | `2026-07-13` |
| `performed` | `true` / `false` |
| `status_phase` | `scheduled` \| `running` \| `completed` |
| `ok` | bool when completed |
| `status` / classified reason | `healthy`, `unavailable`, `rate_limited`, … |
| `attempted_at_epoch` / `completed_at_epoch` | timestamps |
| `latency_ms` / `http_status` / `detail` | as today |

Suggested storage:

- Keep global latest in `trading_gate.json` (`last_probe`, `rest_*`, latch fields).
- Add `probes_by_tranche` map (or sibling `runtime/rest_probes.json`) keyed by `tranche_id` for the session date, plus `startup` record.

Always record failure/timeout (timeout cancellation + failure recording required) so the runner never sees an endless “unknown”.

---

## 8. Other required hardening (keep)

| Item | Requirement |
|------|-------------|
| Shared launcher broker | Owned **only** by the background coordinator |
| Timeout cancellation + failure recording | Hard timeout; always persist result |
| Main-loop stall watchdog | If tick/supervision gap exceeds threshold → CRITICAL |
| Exactly-once `TRANCHE_MISSED` | When window ends, pending/unfired side(s) → one CRITICAL per `slot_key` |
| Avast SSL sanitization | Keep `common/win_ssl_env.py` on all entry points |
| Jul 10 latch / Resume / cooldown / `cooldown_blind` | Unchanged green |
| No automatic catch-up | No fire after `entry_window_end` |

---

## 9. Call path (target)

```text
run.py session start
  ├─ initialize trading_gate for session_date
  ├─ start ProbeCoordinator (daemon)
  │    ├─ async job: startup probe (shared broker)
  │    └─ schedule pre_tranche jobs at window_start − LEAD
  ├─ start streamer / market_data / stop_monitor   ← does NOT wait for probes
  └─ main loop every 5s
       ├─ health / restart children
       ├─ stall watchdog
       └─ EntryMonitorRunner.tick(now)             ← read-only gate
            └─ evaluate_new_risk_gate(tranche_id=lot)
                 └─ read probe state for that tranche (no REST)
```

---

## 10. Implementation plan

### Phase A — Coordinator + non-blocking gate

1. Add `ProbeCoordinator` with shared broker, startup async, pre-tranche schedule.
2. Change `evaluate_new_risk_gate` (or runner wrapper) to take `tranche_id` and **never** call `run_rest_probe` / `get_broker`.
3. Wire `run.py`: start coordinator early; remove sync startup probe from main thread.
4. Persist probe records as in §7.

### Phase B — Miss alert + stall watchdog

1. Exactly-once `TRANCHE_MISSED` per `slot_key`.
2. Main-loop stall watchdog CRITICAL.

### Phase C — Docs / RC

1. Mark this spec Implemented on the feature branch.
2. Update Jul 13 live session follow-ups.
3. RC checklist; **do not merge to `master`** until operator sign-off.

---

## 11. File touch list

| File | Change |
|------|--------|
| `common/probe_coordinator.py` (new) | Background jobs, shared broker, schedule, persistence |
| `common/trading_gate.py` | Non-blocking evaluate with `tranche_id`; probe map fields |
| `common/rest_probe.py` | Timeout/failure always recorded; sources `startup` / `pre_tranche` |
| `run.py` | Start coordinator async; no sync probe before services |
| `blocks/entry/runner.py` | Pass `tranche_id` (= lot); `TRANCHE_MISSED`; no REST |
| `tests/test_probe_coordinator.py` (new) | Budget, dedup, non-blocking, no retry |
| Existing gate / Jul10 / entry runner tests | Stay green |

---

## 12. Required tests

| # | Test |
|---|------|
| T1 | One startup probe per process day |
| T2 | Exactly one automatic probe per tranche |
| T3 | P and C of same tranche share one probe |
| T4 | Six tranches → **seven** routine automatic probes total |
| T5 | Repeated 5s ticks create **no** additional probes |
| T6 | Failed tranche probe is **not** automatically retried |
| T7 | Hanging broker/probe **cannot** block launcher main loop |
| T8 | Probe success allows **both** sides of the tranche |
| T9 | Probe failure blocks **both** sides |
| T10 | Jul 10 gate semantics remain green |

Also retain: pause/skip no miss; miss exactly once after window_end.

### Regression

```bash
pytest tests/test_probe_coordinator.py \
       tests/test_trading_gate.py \
       tests/test_rest_probe.py \
       tests/test_trading_gate_semantics.py \
       tests/test_entry_runner.py \
       tests/test_jul10_gate_replay.py -q
```

---

## 13. Acceptance criteria

| # | Criterion |
|---|-----------|
| A1 | Automatic budget ≤ `1 + N_tranches` (7 for six MEIC slots) |
| A2 | No `REST_KEEP_WARM_*`; no all-day polling probe loop |
| A3 | Main loop / runner never sync-broker or sync-REST |
| A4 | Dedup by `(session_date, strategy, tranche_time)` |
| A5 | Pending probe → `rest_probe_pending`; fail → block + record; no auto-retry |
| A6 | Successful pre_tranche authorizes both sides through window end |
| A7 | `TRANCHE_MISSED` exactly once per overdue `slot_key` |
| A8 | Stall watchdog CRITICAL on main-loop gap |
| A9 | Jul 10 latch / Resume / cooldown / `cooldown_blind` unchanged |
| A10 | Avast SSL sanitize remains |
| A11 | Work lives on `fix/pre-entry-rest-probe-hardening` — **not merged to `master`** in this change |

---

## 14. Rollout

1. Develop and test on `fix/pre-entry-rest-probe-hardening`.
2. Paper / off-hours dry run; confirm ≤7 automatic probes and spawn after pre_tranche success.
3. Pre-market RC after checklist; merge to `master` only on explicit operator approval (**out of scope for this workstream’s merge step**).

**Rollback:** disable coordinator via env flag (e.g. `REST_PROBE_COORDINATOR_ENABLED=false`) only if a documented legacy path remains — prefer fix-forward; do not reintroduce sync `get_broker()` on tick.

---

## 15. Decision log

| Date | Decision |
|------|----------|
| 2026-07-13 | Initial doc after 11-00 miss (included rejected keep-warm idea). |
| 2026-07-13 (amend) | **Normative policy:** 1 startup + 1 probe per tranche (shared P/C); max 7/day; background coordinator; **no keep-warm**; **no auto-retry**; non-blocking main loop; branch `fix/pre-entry-rest-probe-hardening`; no merge to `master` until RC. |
