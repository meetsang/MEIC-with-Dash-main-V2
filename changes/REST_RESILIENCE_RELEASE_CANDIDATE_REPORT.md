# REST Resilience — Integrated Runtime Release Candidate

**Branch:** `fix/phase5-mqtt-entry-fallback`  
**HEAD:** `402f731` (Phase 5) + release-candidate validation artifacts  
**Date:** 2026-07-10  
**Status:** Green — ready to merge as single linear RC (do not merge earlier phase branches separately)

## Commit ancestry

```
402f731 Phase 5: MQTT entry fallback during REST cooldown and low coverage.
d90f4a1 Phase 2 follow-up: sub-second SHA-256 reconcile jitter.
6f3d35d Phase 2: adaptive stop reconcile and REST observability.
69182b0 Phase 4: fill-time software-breach safety with quote provenance gates.
16dde11 Phase 3: MQTT source provenance with session metadata and get_quote().
54030f2 Phase 1: bounded fill sync with provenance and sole stop-monitor ownership.
5150735 master (pre-REST-resilience docs baseline)
```

Phases 1, 2 (incl. jitter follow-up), 3, 4, and 5 are **already linear** on this branch. Merge this branch once; do not cherry-pick or separately merge `fix/phase1-*`, `fix/phase2-*`, etc.

## Working tree / artifact hygiene

Verified **not** staged for merge:

| Path | Status |
|------|--------|
| `runtime/` | Untracked (local broker cooldown / metrics) |
| `trades/test/` | Untracked (local fixtures) |
| `.env` | Not in repo |
| `scripts/_diag_history_today.py` | Untracked diagnostic |
| `changes/BROKER_REST_RESILIENCE_FILL_PROVENANCE_TECH_SPEC.md` | Untracked working doc |
| `changes/REST_FILL_SYNC_ENTRY_FIX_DESIGN.md` | Local edit only (uncommitted) |

Release-candidate additions: `tests/test_rest_resilience_release_candidate.py`, `tests/conftest.py` (cooldown isolation), this report.

## Full test result

```bash
uv run pytest tests/ -q --ignore=tests/integration
→ 524 passed
```

Focused suites (Phases 1–5 + entry + reconcile + breach): all green in integrated run.

## Integrated paper scenario (`tests/test_rest_resilience_release_candidate.py`)

| Scenario | Result |
|----------|--------|
| Normal REST entry (`candidate_source=rest`) | Pass |
| Forced REST cooldown → zero entry REST calls → MQTT fallback | Pass |
| Valid post-scan MQTT fallback selects spread | Pass |
| Missing-leg protective estimate promotes `open` | Pass |
| Immediate exchange-stop placement (independent of fill grace) | Pass |
| Fill grace blocks breach; 2 advancing observations confirm | Pass |
| Exchange-stop fill alert enqueues exit handler | Pass |
| Old stream session rejected after restart | Pass |
| Dashboard `/api/summary` refresh (patched read-only paths) | Pass |
| Production default env constants | Pass |

Long chase / spread-close paths covered by existing `test_v3_paper_scenarios`, `test_long_close_chase`, `test_closing_orphan_recovery` (all green in full suite).

## REST before / after summary

### Working-stop reconcile (8 open trades, 10 min, healthy MQTT)

| Metric | Before (fixed 10s) | After (15–20s + SHA-256 jitter) |
|--------|-------------------|-----------------------------------|
| Total reconcile calls | 488 | 268 (−45%) |
| Peak calls in one second | 8 | 4 |

### Entry REST during cooldown

| Metric | Before Phase 5 | After Phase 5 |
|--------|----------------|---------------|
| REST `entry_market_data_rest` calls during cooldown | N (attempted, failed) | **0** |
| MQTT fallback selection | Not available | Yes, with post-scan gating |

## Operational metrics (representative RC snapshot)

From integrated metrics collection + Phase 2 simulation:

```json
{
  "rest_by_operation": {
    "pending_fill_status": 1,
    "working_stop_reconcile": 1,
    "entry_market_data_rest": 1
  },
  "rest_by_priority": { "HIGH": 1, "NORMAL": 1, "LOW": 1 },
  "cooldown_skips": { "entry_market_data_rest:NORMAL": 1 },
  "reconcile_before_peak": 8,
  "reconcile_after_peak": 4,
  "reconcile_before_total": 488,
  "reconcile_after_total": 268,
  "entry_rest_avoided_cooldown": 1
}
```

MQTT pair rejection reasons tracked per scan in `EntryScanDiagnostics.mqtt_rejection_reasons` (e.g. `pre_scan`, `pair_skew`, `old_session`, `source_stale`).

## MQTT safety results

| Gate | Enforced |
|------|----------|
| Phase 3 `QuoteSnapshot` provenance | Yes |
| Post-`scan_request_epoch` quotes (entry) | Yes |
| Current session only | Yes |
| No replay / pre-subscription / pre-scan | Yes |
| Homogeneous REST or MQTT pair (no mixed legs) | Yes |
| Phase 4 fill grace + consecutive breach confirmation | Yes |
| `TT_LEGACY_REPUBLISH_LAST_MIDS=false` default | Yes |

## Production defaults verified

| Setting | Expected | Verified |
|---------|----------|----------|
| `TT_LEGACY_REPUBLISH_LAST_MIDS` | `false` | `legacy_republish_enabled() is False` |
| `ENTRY_MQTT_FALLBACK_ENABLED` | `true` | Default `True` |
| `BREACH_FILL_GRACE_SEC` | `10` | `10.0` |
| `BREACH_CONFIRM_OBSERVATIONS` | `2` | `2` |
| `STOP_RECONCILE_OPEN_SEC` | `15` | `15.0` |
| `STOP_RECONCILE_OPEN_JITTER_SEC` | `5` | `5.0` |

## Known limitations

- MQTT entry fallback requires live streamer + `broker._prices` cache; partial REST without cache still uses legacy REST-only path.
- Per-process REST metrics (`runtime/rest_metrics_<pid>.json`) are not aggregated cross-process.
- Dashboard `/api/broker_health` reads metrics snapshots only; no broker calls (unchanged).
- Phase 6 presentation (Estimated Fill, Expired, quote-source badges) is **out of scope** for this RC merge.

## Rollback settings

Disable new behavior without code revert:

```text
ENTRY_MQTT_FALLBACK_ENABLED=false
STOP_RECONCILE_OPEN_SEC=10
STOP_RECONCILE_OPEN_JITTER_SEC=0
BREACH_FILL_GRACE_SEC=0
BREACH_CONFIRM_OBSERVATIONS=1
TT_LEGACY_REPUBLISH_LAST_MIDS=true
```

Full code rollback: merge revert of `fix/phase5-mqtt-entry-fallback` onto `master`.

## Recommended merge commands

```bash
git checkout master
git pull origin master
git merge --no-ff fix/phase5-mqtt-entry-fallback -m "Merge REST resilience RC: fill provenance, adaptive reconcile, MQTT provenance, breach safety, entry fallback."
git push origin master
```

Do **not** separately merge `fix/phase1-fill-provenance-bounded-sync`, `fix/phase2-adaptive-stop-reconcile-rest-observability`, `fix/phase3-mqtt-source-provenance`, or `fix/phase4-fill-time-breach-safety` — their commits are ancestors of `402f731`.

---

**Phase 6** (`fix/phase6-dashboard-runtime-status`) is a separate presentation-only branch; merge after RC if desired.
