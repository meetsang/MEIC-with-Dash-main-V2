# Phase 6 — Dashboard Runtime Status (Presentation Only)

**Branch:** `fix/phase6-dashboard-runtime-status`  
**Base:** `4a2cacb` (REST resilience release-candidate validation on `fix/phase5-mqtt-entry-fallback`)  
**Date:** 2026-07-10  
**Status:** Green — presentation-only; trading logic unchanged

## Scope

Display-only dashboard enhancements:

| Feature | Implementation |
|---------|----------------|
| Protective-estimate provenance | `Estimated Fill · …` prefix via `decorate_entry_label()` |
| Expired trades | Slot/manual state `expired` when `settled_at_expiry` or `close_mechanism=expiry_settlement` |
| Entry quote source | Optional `REST` / `MQTT fallback` sub-label under entry credit |
| Software-breach readiness | Optional sub-label under breach column from `breach_watch` |

**Out of scope (unchanged):** trading logic, thresholds, quote selection, stop logic, REST scheduling. Dashboard GET/read routes remain broker-free and mutation-free.

## Files

- `dashboard/runtime_display.py` — read-only label helpers
- `dashboard/server.py` — overlay + slot state `expired`
- `dashboard/manual_spread_handlers.py` — manual grid labels
- `dashboard/templates/index.html` — `Expired` state styling + sub-labels
- `tests/test_phase6_dashboard_runtime_status.py`

## Test result

```bash
uv run pytest tests/ -q --ignore=tests/integration
→ 530 passed (524 RC baseline + 6 Phase 6)
```

Phase 6 tests cover protective-estimate label, expired state, quote source labels, breach readiness, and zero broker calls on `/api/summary` + `/api/broker_health`.

## Merge guidance

Merge **runtime RC first**, then Phase 6:

```bash
git checkout master
git pull origin master
git merge --no-ff fix/phase5-mqtt-entry-fallback
git merge --no-ff fix/phase6-dashboard-runtime-status
```

Do not merge earlier phase branches separately; their commits are already in the RC branch ancestry.
