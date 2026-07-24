# Account-Wide REST Governor — Implementation Report

**Branch:** `fix/account-wide-rest-governor`  
**Date:** 2026-07-24  
**Base:** `master` @ `fd7115b`  
**Do not merge to master** until operator RC sign-off.

## Summary

Replaces the per-process-only REST token bucket with an account-wide,
cross-process governor (Windows `msvcrt` / POSIX `fcntl` file locks), adds
real HIGH/NORMAL/LOW scheduling under the global rate ceiling, hardens
429/auth retry policy, probe deadlines, manual Take Trade probing, shared
launcher brokers for entry workers, and batched stop-monitor peaceful
reconcile.

## Preserved behavior

- Background `ProbeCoordinator` (1 startup + 1 per MEIC tranche; P/C share)
- No keep-warm / no automatic probe retry
- Cooldown + latch / Resume / `cooldown_blind`
- MQTT entry fallback, fill provenance, stop safety paths

## Simulated call-rate evidence (12 open spreads)

| Metric | Before | After |
|--------|--------|-------|
| Peaceful reconcile pattern | Per-trade `get_order_status` → up to N× live-orders | One `get_live_orders` snapshot / cycle; `get_order` only for ids absent from snapshot |
| Calls / minute (interval ≈ 17.5s) | **~41.1** (`12 × 60/17.5`) | **~3.4** (`1 × 60/17.5`) |
| Reduction | — | **~12×** (~92%) |

Aggregate governor counters exercised in
`tests/test_account_wide_rest_governor.py::TestSimulatedAggregateCallRate`
with **zero live broker I/O** (`MockBroker` / MagicMock / local governor files only).

## Test results

- Focused governor suite + related: **51 passed**
- Full `tests/`: **609 passed** (2 pre-existing Windows heartbeat warnings)

## Confirmation

- No live broker calls were made during tests (`get_broker` blocked under pytest;
  mocks/`MagicMock` only).
- `master` was not modified (work committed only on `fix/account-wide-rest-governor`).
