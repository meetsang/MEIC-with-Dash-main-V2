# Phase 5: MQTT Entry Fallback — Report

**Branch:** `fix/phase5-mqtt-entry-fallback`  
**Base commit:** `d90f4a1` (Phase 2 follow-up: sub-second SHA-256 jitter)  
**Date:** 2026-07-10

## Summary

Phase 5 adds REST-to-MQTT option quote fallback for API entry scans during broker cooldown, REST 429/low coverage, using Phase 3 `QuoteSnapshot` provenance and entry-specific pair validation aligned with Phase 4 rules. Strike/credit selection logic is unchanged — only the quote source may switch to `mqtt_fallback`.

## Files changed

| File | Change |
|------|--------|
| `blocks/entry/entry_scan_config.py` | **New** — env-tuned fallback and MQTT entry freshness defaults |
| `blocks/entry/entry_quote_validation.py` | **New** — post-scan MQTT pair validation (`scan_request_epoch`, session, skew, spread width) |
| `blocks/entry/mqtt_entry_fallback.py` | **New** — REST attempt with cooldown guard, MQTT registration/wait, diagnostics |
| `blocks/entry/spread_scan.py` | API scan integrates fallback; `SpreadCandidate.candidate_source`; overlap shift MQTT-safe |
| `brokers/tastytrade_broker.py` | `fetch_option_mids_api` tagged `entry_market_data_rest` NORMAL |
| `common/rest_operations.py` | `OPERATION_ENTRY_MARKET_DATA_REST` |
| `common/rest_metrics.py` | Known operation `entry_market_data_rest` |
| `tests/test_mqtt_entry_fallback_phase5.py` | **New** — Phase 5 contract + Jul 9 01-45 counterfactual tests |
| `tests/test_spread_scan_api.py` | Autouse disable fallback for legacy partial-REST API tests |
| `.env.example` | Entry MQTT fallback env vars |

## Fallback decision flow

```
quote_source == 'api' && ENTRY_MQTT_FALLBACK_ENABLED
  │
  ├─ cooldown active before REST?
  │    └─ yes → zero REST calls, record skipped_cooldown, MQTT fallback
  │
  ├─ attempt REST entry_market_data_rest (batched)
  │    ├─ 429 / rate limit → halt remaining batches → MQTT fallback
  │    └─ coverage = valid_positive_prices / requested
  │         ├─ coverage ≥ ENTRY_REST_MIN_COVERAGE_PCT (50%) → REST path (`candidate_source=rest`)
  │         └─ coverage < 50%
  │              ├─ broker has `_prices` MQTT cache → MQTT fallback
  │              └─ no MQTT cache → partial REST (legacy tests / no streamer)
  │
  └─ MQTT fallback
       1. record scan_request_epoch
       2. register scan symbols (streaming, not REST)
       3. wait up to ENTRY_MQTT_READY_TIMEOUT_SEC (bounded 0.25s polling)
       4. evaluate pairs via get_quote() + entry validation
       5. apply unchanged credit band / strike selection
```

## Quote validation rules (MQTT fallback)

Both legs must pass via `MqttPriceCache.get_quote()`:

| Gate | Rule |
|------|------|
| Session | Current `stream_session_id` |
| Event kind | Genuine DXLink (not replay) |
| Post-scan | `source_event_epoch >= scan_request_epoch` when `MQTT_REQUIRE_POST_SCAN_QUOTE=true` |
| Post-subscription | `source_event_epoch >= subscription_epoch` |
| Age | `< MAX_MQTT_ENTRY_QUOTE_AGE_SEC` (10s) |
| Skew | `≤ MAX_MQTT_ENTRY_PAIR_SKEW_SEC` (2s) |
| Prices | Positive; spread ≥ 0; spread ≤ width + tolerance |

Rejection reasons tracked in `mqtt_rejection_reasons` (e.g. `pre_scan`, `pair_skew`, `source_stale`).

## REST vs MQTT source consistency

- Each candidate uses **both REST legs** (`candidate_source=rest`) or **both MQTT legs** (`candidate_source=mqtt_fallback`).
- No mixed REST/MQTT pair is accepted.
- Low-coverage REST maps are **not** blended with MQTT mids.

## Jul 9 01-45 counterfactual

| Scenario | Result |
|----------|--------|
| REST cooldown active + valid post-scan MQTT pair @ OTM 70 | **Trade selected** (`candidate_source=mqtt_fallback`) |
| REST cooldown + pre-request / stale MQTT only | **No trade** (empty scan + diagnostic failure line) |

Simulated diagnostic example:

```text
entry_scan_failed source=rest_then_mqtt rest_coverage=0/67 cooldown=true
mqtt_current_session=63/67 mqtt_post_scan=61/67 mqtt_valid_pairs=0 reason=pair_skew
```

## REST calls avoided during cooldown

When cooldown is active before scan:

- `attempt_rest_entry_quotes` makes **zero** `fetch_option_mids_api` calls
- `entry_market_data_rest` REST metric count stays **0**
- `skipped_cooldown` incremented instead
- MQTT cache reads are **not** counted as REST

## Phase 2 jitter follow-up (same branch ancestry)

Sub-second SHA-256 jitter refinement on Phase 2 (`d90f4a1`):

| Metric | Before (fixed 10s) | After (15–20s + fractional jitter) |
|--------|-------------------|--------------------------------------|
| Total reconcile calls (8 trades / 10 min) | 488 | 268 |
| Peak calls per one-second bucket | 8 | **4** |

## Test results

### Focused Phase 5 + entry + provenance + breach + fill-sync + reconcile

```
uv run pytest tests/test_mqtt_entry_fallback_phase5.py \
  tests/test_spread_scan_api.py tests/test_credit_entry.py \
  tests/test_mqtt_source_provenance_phase3.py \
  tests/test_fill_time_breach_safety_phase4.py \
  tests/test_fill_provenance_phase1.py \
  tests/test_adaptive_reconcile_phase2.py -q
→ 107 passed
```

### Phase 5 contract coverage (23 tests in phase5 file + shared regressions)

All required scenarios covered including cooldown zero REST, post-scan MQTT selection, pre-scan rejection, 429 halt, coverage threshold, homogenous sources, validation gates, Jul 9 counterfactuals, no CSV reads, REST metrics unchanged during MQTT fallback.

### Full non-integration suite

```
uv run pytest tests/ -q --ignore=tests/integration
→ 510 passed
```

## Out of scope (deferred)

Dashboard badges, Expired display, broker gateway refactor, breach threshold changes, entry strategy/credit rule changes, sampled CSV/historical ladder reads for live entry.
