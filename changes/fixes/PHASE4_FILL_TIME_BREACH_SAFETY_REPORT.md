# Phase 4: Fill-Time Software-Breach Safety — Report

**Branch:** `fix/phase4-fill-time-breach-safety`  
**Base commit:** `16dde11` (Phase 3)  
**Date:** 2026-07-10

## Summary

Phase 4 gates software breach detection on validated `QuoteSnapshot` provenance from Phase 3. Exchange-stop placement remains immediate after fill; only software breach evaluation waits for fill grace, quote readiness, and consecutive confirmation.

## Files changed

| File | Change |
|------|--------|
| `blocks/stop/breach_config.py` | **New** — env-tuned breach readiness/confirmation defaults |
| `blocks/stop/fill_reference.py` | **New** — `fill_reference_epoch` resolution and persistence |
| `blocks/stop/breach_quote.py` | **New** — quote-pair readiness, confirmation state machine, `evaluate_software_breach_exit()` |
| `blocks/stop/breach_watch.py` | Extended snapshot fields + rate-limited readiness diagnostics |
| `blocks/stop/phases.py` | Phase 1 `_exit_required` / `execute` use provenance + confirmation |
| `blocks/stop/monitor.py` | Provenance-aware `_refresh_breach_watch`; display-only refresh during exit jobs |
| `blocks/stop/fill_provenance.py` | Persist `fill_reference_epoch` on fill-sync resolution |
| `blocks/stop/v3/supervisor.py` | F-8 arm gate uses `software_breach_ready`; refresh watch during exit jobs |
| `common/mqtt_prices.py` | `last_event_kind`, `current_stream_session_id`, `allow_pre_subscription` on `get_quote()` |
| `tests/test_fill_time_breach_safety_phase4.py` | **New** — 29 Phase 4 contract + replay tests |
| `tests/test_v3_paper_scenarios.py` | Mock price cache exposes `get_quote` / session helpers |
| `.env.example` | Breach readiness env vars documented |

## Phase 3 metadata contract (verified before breach changes)

| Check | Result |
|-------|--------|
| `source_event_epoch` in Unix seconds (not ms) | Verified in `test_source_event_epoch_is_unix_seconds_not_milliseconds` |
| Advances only on genuine DXLink events | Verified |
| Heartbeat does not advance source epoch/sequence | Verified |
| Replay does not advance source epoch | Verified |
| `stream_session_id` must match current session | Verified |

## Readiness rules

Software breach is **ineligible** until all pass:

1. `now >= fill_reference_epoch + BREACH_FILL_GRACE_SEC` (default 10s)
2. Short and long quotes from `get_quote()` with genuine `event_kind`
3. Current streamer session (not replay / old session)
4. Source timestamps after `fill_reference_epoch` and after per-leg `subscription_epoch`
5. Each leg younger than `MAX_MQTT_BREACH_QUOTE_AGE_SEC` (default 5s)
6. Pair skew ≤ `MAX_MQTT_PAIR_SKEW_SEC` (default 2s)
7. Positive prices, nonnegative spread, spread ≤ width + `MQTT_SPREAD_WIDTH_TOLERANCE`

`fill_reference_epoch` resolution order:

1. Brokerage `filled_at` (when provided to `ensure_fill_reference_epoch`)
2. Fill-sync `resolved_at_epoch`
3. `open_order.last_sync_epoch`
4. Entry timestamp (controlled fallback)

Persisted in `lifecycle.fill_reference_epoch` + `fill_reference_source`.

## Confirmation / reset behavior

- Requires `BREACH_CONFIRM_OBSERVATIONS` (default 2) valid breached pairs within `BREACH_CONFIRM_MAX_WINDOW_SEC` (default 3s)
- Second observation must advance at least one leg's sequence or `source_event_epoch`
- Re-evaluating the same sequences does not increment confirmation
- Resets on: spread below threshold, invalid/stale quote, pair skew failure, session change, no quote advancement, window expiry, phase/state change

Only after confirmation may Phase 1 call `replace_with_limit_close()` (existing cancel-confirm path unchanged).

## Exchange-stop placement

**Not delayed.** `_ensure_stop_for_filled_qty()` / `setup_initial_stop()` run independently of fill grace and quote readiness. Verified: `test_exchange_stop_placed_immediately_during_fill_grace`.

## Duplicate exit prevention

Active V3 exit jobs refresh `breach_watch` display only (`_refresh_breach_watch_display_only`) and do not re-enqueue breach exits. Verified: `test_existing_exit_job_prevents_duplicate_exit_creation`.

## Jul 9 `11-00_P` false-breach replay

| Scenario | Result |
|----------|--------|
| Pre-fill stale spread ~$2.90 (short 3.70 / long 0.80, source before fill) | **Rejected** — `pre_fill`; no software breach |
| Post-fill coherent spread ~$1.05 (short 1.85 / long 0.80) | **No breach** — below 2× credit threshold |
| Genuine later breach with two advancing coherent pairs | **Triggers** after confirmation |

Exchange stop would remain working throughout missing/invalid quote periods (no cancel on stale scalar fallback).

## `breach_watch` extensions

New fields: `quote_pair_valid`, `quote_pair_reason`, `software_breach_ready`, `short_source_epoch`, `long_source_epoch`, `short_sequence`, `long_sequence`, `pair_skew_sec`, `stream_session_id`, `fill_reference_epoch`, `fill_grace_remaining_sec`, `breach_confirmation_count`, `breach_confirmation_required`, `breach_confirmation_reason`, `software_breach_confirmed`.

Display refresh continues during active exit jobs; breach **execution** is separated via `evaluate_transitions=False` on display-only refresh.

## Test results

### Focused Phase 3 + Phase 4 MQTT/breach

```
uv run pytest tests/test_fill_time_breach_safety_phase4.py \
  tests/test_mqtt_source_provenance_phase3.py \
  tests/test_breach_watch.py tests/test_v3_incident_fixes.py \
  tests/test_phase1_breach.py tests/test_v3_software_breach.py \
  tests/test_streamer_stale.py -q
→ 75 passed
```

### Full non-integration suite

```
uv run pytest tests/ -q --ignore=tests/integration
→ 463 passed
```

## Out of scope (deferred)

MQTT entry fallback, adaptive stop reconciliation, REST metrics, dashboard visual badges, expired display changes.
