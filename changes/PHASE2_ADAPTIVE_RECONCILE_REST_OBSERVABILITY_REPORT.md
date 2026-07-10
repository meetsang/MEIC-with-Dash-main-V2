# Phase 2: Adaptive Stop Reconcile & REST Observability — Report

**Branch:** `fix/phase2-adaptive-stop-reconcile-rest-observability`  
**Base commit:** `69182b0` (Phase 4)  
**Date:** 2026-07-10

## Summary

Phase 2 introduces a shared adaptive working-stop reconcile policy (V2 + V3), deterministic per-trade jitter, REST operation/priority classification, cooldown-aware LOW reconcile deferral, and lightweight per-process REST metrics exposed via health JSON — without slowing safety-critical paths.

## Files changed

| File | Change |
|------|--------|
| `blocks/stop/reconcile_policy.py` | **New** — `reconcile_interval_sec()`, CRC32 jitter, due/scheduling helpers, 8-trade simulation |
| `common/rest_operations.py` | **New** — operation names + HIGH/NORMAL/LOW priority map |
| `common/rest_metrics.py` | **New** — thread-safe bounded metrics, `runtime/rest_metrics_<pid>.json` writer |
| `blocks/stop/monitor.py` | Adaptive reconcile replaces fixed 10s gate; operation tags on broker calls |
| `blocks/stop/v3/supervisor.py` | Adaptive reconcile + cooldown deferral; metrics snapshot on heartbeat |
| `blocks/stop/fill_sync.py` | Pending fill remains HIGH; fill audit respects cooldown skip |
| `blocks/stop/fill_provenance.py` | Fill audit tagged LOW `fill_audit` |
| `blocks/stop/v3/recovery.py` | Recovery reconcile tagged HIGH `recovery_reconcile` |
| `common/rest_limiter.py` | Records calls into REST metrics; `stats()` embeds metrics snapshot |
| `brokers/base.py` | `get_order_status(..., *, priority, op)` |
| `brokers/tastytrade_broker.py` | Metrics on call/skip/failure/429; priority-aware `get_order_status` |
| `dashboard/server.py` | `/api/broker_health` adds read-only `rest_metrics` (no broker I/O) |
| `tests/test_adaptive_reconcile_phase2.py` | **New** — 23 Phase 2 contract tests |
| `.env.example` | `STOP_RECONCILE_*` env vars documented |
| Test compatibility | `mock_broker`, `test_fill_provenance_phase1`, `test_closing_orphan_recovery`, `test_breach_close`, `test_dual_manual_kill_simulation` accept `priority`/`op` kwargs |

## Interval policy

Env defaults:

```text
STOP_RECONCILE_OPEN_SEC=15
STOP_RECONCILE_OPEN_JITTER_SEC=5
STOP_RECONCILE_STALE_SEC=10
STOP_RECONCILE_CLOSING_SEC=5
```

| Trade condition | REST reconcile behavior |
|-----------------|-------------------------|
| Open + MQTT healthy | `15 + CRC32(strategy\|lot\|side\|active_stop_order_id)` jitter in `[0, 5]` → **15–20s** |
| Open + MQTT stale/unhealthy | **10s** (`STOP_RECONCILE_STALE_SEC`) |
| Closing / close-only mode | **5s** |
| Exit job active | Peaceful reconcile **skipped** (existing fast close polling unchanged) |
| Long chase active | Peaceful reconcile **skipped** (FAST_INTERVAL 3s unchanged) |
| Recovery / cancel uncertainty | Interval **0** (immediate safety path) |
| Pending fill sync | Phase 1 budget unchanged (`FILL_SYNC_FAST_SEC=3`, HIGH `pending_fill_status`) |
| Exchange-stop / alert fill | Immediate HIGH `alert_confirm` (not gated by peaceful interval) |

Jitter is **stable across restarts** for the same trade identity; changes only when `active_stop_order_id` changes.

**Not slowed:** exchange-stop placement, stop cancel confirmation, spread-close polling, long-leg chase, recovery, broker fill alerts, V3 0.25s supervisor cycle, Phase 4 MQTT breach evaluation.

## Priority mapping

| Priority | Operations |
|----------|------------|
| **HIGH** | `pending_fill_status`, `spread_close_status`, `long_close_status`, `place_stop_order`, `cancel_order`, `replace_stop_order`, `emergency_close`, `recovery_reconcile`, `alert_confirm`, spread/close placement |
| **NORMAL** | `entry_market_data`, `get_order`, `get_positions`, `manual_lookup` |
| **LOW** | `working_stop_reconcile`, `fill_audit`, `get_live_orders` |

Safety-critical calls are never silently downgraded.

## Cooldown behavior

During broker cooldown (`broker_cooldown.should_skip_priority`):

- **HIGH** — permitted (existing safety rules)
- **NORMAL** — existing cooldown policy
- **LOW** peaceful `working_stop_reconcile` — **skipped**, but `schedule_next_working_stop_reconcile()` still advances `lifecycle.next_working_stop_reconcile_epoch` so the V3 supervisor does not retry every 0.25s
- Closing, recovery, cancel confirmation, and long chase use HIGH and are **not** blocked by peaceful-open interval

## REST metrics

Per-process, thread-safe, bounded-memory collector (`common/rest_metrics.py`):

```json
{
  "scope": "per_process",
  "window_start_epoch": 0,
  "calls_last_1m": 0,
  "calls_last_5m": 0,
  "by_operation": { "working_stop_reconcile": 0, "...": 0 },
  "by_priority": { "HIGH": 0, "NORMAL": 0, "LOW": 0 },
  "skipped_cooldown": {},
  "failed": {},
  "last_429_epoch": null
}
```

- Metrics collection makes **no** broker calls
- Failures in metrics paths are swallowed (debug log only)
- Snapshot written atomically to `runtime/rest_metrics_<pid>.json` on supervisor heartbeat
- Dashboard `/api/broker_health` reads snapshot only

## 8-trade / 10-minute simulation (healthy MQTT, no closes)

Deterministic simulation via `simulate_reconcile_events()`:

| Metric | Before (fixed 10s) | After (15–20s + jitter) |
|--------|-------------------|-------------------------|
| Total reconcile calls | **488** | **271** (−44%) |
| Peak calls in one second | **8** (all trades aligned at t=0, 10, 20, …) | **6** (staggered first due at 17–18s; reduced alignment) |

Per-trade jitter sample (8 open trades): `[2.03, 2.72, 1.98, 2.68, 1.95, 2.80, 1.91, 2.76]` seconds added to 15s base.

### Safety-path spot checks (simulation + tests)

| Path | Interval / behavior | Verified |
|------|---------------------|----------|
| One trade entering closing | 5s | `test_closing_trade_uses_5_second_interval` |
| Long chase | FAST_INTERVAL 3s; reconcile skipped | `test_long_chase_active_skips_peaceful_reconcile_due` |
| Recovery / cancel uncertainty | 0s (immediate) | `test_recovery_active_uses_immediate_interval` |
| Broker cooldown + LOW reconcile | Skipped + rescheduled | `test_low_reconcile_skipped_during_cooldown_and_rescheduled` |
| HIGH during cooldown | Permitted | `test_high_call_permitted_during_cooldown` |
| Phase 4 breach loop | Unchanged import/cadence | `test_phase4_breach_loop_unchanged_import` + Phase 4 suite green |
| Phase 1 fill sync budget | `FILL_SYNC_FAST_SEC=3` unchanged | `test_phase1_fill_sync_budget_unchanged` + Phase 1 suite green |

## Test results

### Focused Phase 1 + Phase 2 + Phase 3 + Phase 4 + broker hardening + V3 + close recovery

```
uv run pytest tests/test_adaptive_reconcile_phase2.py \
  tests/test_fill_provenance_phase1.py \
  tests/test_mqtt_source_provenance_phase3.py \
  tests/test_fill_time_breach_safety_phase4.py \
  tests/test_broker_hardening.py tests/test_v3_remaining.py \
  tests/test_closing_orphan_recovery.py -q
→ 104 passed
```

### Phase 2 contract tests (23)

All tests in `tests/test_adaptive_reconcile_phase2.py` pass, covering:

1. Healthy open 15+jitter  
2. Same jitter after restart  
3. Different identities distribute jitter  
4. CRC32 not Python `hash()`  
5. MQTT-stale → 10s  
6. Closing → 5s  
7. Close-only → fast  
8. Exit job unchanged  
9. Long chase unchanged  
10. Recovery fast  
11. Alert/recovery immediate path  
12. Phase 4 breach unchanged  
13–14. LOW skipped + rescheduled during cooldown  
15. HIGH permitted during cooldown  
16–18. Metrics operation/priority, failures/429, thread-safe bounded  
19. Dashboard metrics zero broker calls  
20. Eight trades staggered  
21. Phase 1 fill sync budget unchanged  
22. Phase 4 false-breach tests green (in suite)  
23. V2/V3 equivalent reconcile policy  

### Full non-integration suite

```
uv run pytest tests/ -q --ignore=tests/integration
→ 486 passed
```

## Out of scope (deferred)

MQTT entry fallback, entry strike selection, dashboard badges, expired display state, global cross-process REST gateway, major broker-session refactoring.
