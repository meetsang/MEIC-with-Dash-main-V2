# Phase 1 Report — Fill Provenance & Bounded Fill Sync

**Date:** 2026-07-10  
**Branch:** `master` (commit on dedicated Phase 1 changeset)  
**Spec:** `changes/BROKER_REST_RESILIENCE_FILL_PROVENANCE_TECH_SPEC.md` §7–§9, §21 Phase 1

## Summary

Phase 1 closes the Jul 9 `01-15_P` class failure: a fully filled spread with one broker leg and an empty long `fills[]` API now receives a **protective estimate** after a bounded **fast + confirm** poll sequence, promotes to `status=open`, and stops opening-order polling. Fill sync is owned solely by the stop monitor (V3 supervisor + V2 runner); launcher, dashboard, and manual dashboard paths no longer call the broker for fill sync.

## Files changed

### Implementation

| File | Change |
|------|--------|
| `blocks/stop/fill_provenance.py` | **New** — provenance helpers, protective estimate, audit, state-machine helpers |
| `blocks/stop/fill_sync.py` | Bounded state machine (`fast` → `confirm_pending` → `resolved_*`), no blind limit-as-fill |
| `blocks/stop/pending_fill_sync.py` | Terminal-phase awareness, audit scheduling |
| `blocks/stop/state.py` | `open_order.fill_sync` schema v2 on pending handshake |
| `blocks/stop/v3/supervisor.py` | Fill sync every discovery cycle (per-trade `next_poll_epoch` throttles REST) |
| `brokers/base.py` | `OrderResult` provenance fields |
| `brokers/tastytrade_broker.py` | `filled_price_source`, `order_limit_price`; tag limit fallback |
| `run.py` | Removed launcher `sync_pending_fills` loop |
| `dashboard/server.py` | Removed `maybe_sync_active_trades` from passive reads |
| `dashboard/manual_spread_handlers.py` | Removed manual dashboard fill sync |
| `manual_spread/entry.py` | Removed post-place `sync_open_order` (stop monitor owns sync) |

### Tests & fixtures

| File | Change |
|------|--------|
| `tests/fill_sync_fixtures.py` | **New** — same-day CT expiry symbols, `same_day_trade_env()` |
| `tests/test_fill_provenance_phase1.py` | **New** — Jul 9 scenario, ownership, audit, stop placement |
| `tests/test_partial_fill_stop.py` | Dynamic expiry + `same_day_trade_env` (fixes `260622` drift) |
| `tests/test_build_manual_trades.py` | Drop obsolete sync mock |
| `tests/conftest.py` | Neutralize stale SPXW expiry in StopMonitor tests (calendar drift) |
| `tests/test_broker_stopped_trading.py` | Same-day symbols + `recovery` block |
| `tests/test_expiry_gate.py` | Stable settlement mock for monitor integration |
| `tests/test_stop_monitor_0dte_freeze.py` | Patch broker window + `recovery` block |

## Test commands and results

### Focused Phase 1 suite (49 tests)

```bash
python -m pytest \
  tests/test_fill_sync.py \
  tests/test_fill_provenance_phase1.py \
  tests/test_pending_fill_sync.py \
  tests/test_partial_fill_stop.py \
  tests/test_broker_hardening.py \
  tests/test_stop_runner_gate.py \
  tests/test_build_manual_trades.py \
  -q
```

**Result:** 49 passed

### Full repository suite

```bash
python -m pytest tests/ -q --ignore=tests/integration
```

**Result:** 419 passed, 2 warnings (pre-existing Windows heartbeat race + async mock warning)

## Explicit coverage (Phase 1 requirements)

| Requirement | Test |
|-------------|------|
| Protective estimate → `status=open` | `TestJul9MissingLegScenario.test_protective_estimate_promotes_open` |
| Stop supervisor places exchange stop | `TestProtectiveEstimateStopPlacement.test_stop_placed_after_protective_estimate` |
| Launcher zero fill-sync broker calls | `TestOwnershipPassiveReads.test_launcher_has_no_fill_sync_loop` |
| Dashboard zero automatic fill-sync calls | `TestOwnershipPassiveReads.test_read_active_trades_makes_no_broker_calls` |
| Manual dashboard zero broker calls | `TestOwnershipPassiveReads.test_manual_dashboard_build_makes_no_broker_calls` |
| Confirm survives restart, at most once | `TestJul9MissingLegScenario.test_confirm_survives_restart_at_most_once` |
| Estimated resolution ≤ one audit | `TestFillProvenance.test_estimated_resolution_at_most_one_audit` |
| Exact resolution performs no audit | `TestFillProvenance.test_exact_resolution_no_audit` |
| Resolved cycles do not duplicate fill history | `TestFillProvenance.test_resolved_cycles_no_duplicate_fill_history` |
| Broker correction does not duplicate active stop | `TestFillProvenance.test_audit_correction_does_not_duplicate_active_stop` |
| Polling stops after resolution | `TestJul9MissingLegScenario.test_no_afternoon_polling_after_resolution` |

## Simulated REST budget — Jul 9 missing-leg case

Scenario: `01-15_P` — short `1.15`, long API empty, limit `0.75`, order `filled`.

| Phase | `get_order_status` calls |
|-------|--------------------------|
| Fast poll (enter `confirm_pending`) | 1 |
| Confirm poll (protective estimate → `resolved_estimated`) | 1 |
| Post-resolution cycles (50× `sync_open_order` + `sync_pending_fills`) | 0 additional |
| **Total** | **2** |

Test: `TestJul9MissingLegScenario.test_jul9_rest_call_budget`  
Operator target (Q9): ≤ 6 opening-order status calls per trade over a 2h session.

## Opening-order polling after resolution

After `resolved_exact` or `resolved_estimated`:

- `open_order.fill_sync.next_poll_epoch` is set to `null`
- `needs_open_order_sync()` returns `False`
- `sync_open_order()` returns immediately when `is_fill_sync_terminal()` is true
- Optional one-time audit (`resolved_estimated` only) uses `maybe_run_fill_audit()` and then `audit_complete`

## Not in this commit (later phases)

- Phase 2: adaptive stop reconcile / REST metrics
- Phase 3: MQTT `__META` / `__HEARTBEAT`, remove `last_mids` republish
- Phase 4: fill-time breach grace
- Phase 5: MQTT entry fallback
- Phase 6: dashboard badges
