# Phase 3: MQTT Source Provenance — Report

**Branch:** `fix/phase3-mqtt-source-provenance`  
**Base commit:** `54030f2` (Phase 1)  
**Date:** 2026-07-10

## Summary

Phase 3 adds MQTT quote provenance for trading decisions while preserving backward-compatible scalar topics. Genuine DXLink quote/trade events publish retained scalars plus per-symbol `__META`. Liveness uses `__SESSION` and `__HEARTBEAT` instead of the removed five-second `last_mids` scalar republish loop.

## Files changed

| File | Change |
|------|--------|
| `common/market_quote.py` | **New** — `QuoteSnapshot`, genuine/replay event kinds, pre-subscription check |
| `common/mqtt_stream_provenance.py` | **New** — topic helpers, session ID, `StreamPublishState`, rollback env |
| `common/mqtt_prices.py` | Parse `__META` / `__SESSION` / `__HEARTBEAT`; `get_quote()`; provenance-aware tick routing |
| `common/streamer_health.py` | Optional `stream_session_id` in health payload |
| `streaming/publish_tastytrade.py` | Genuine-event publish with meta; heartbeat/session; remove normal `last_mids` republish |
| `tests/test_mqtt_source_provenance_phase3.py` | **New** — 16 Phase 3 contract tests |

**Not committed:** `runtime/`, `trades/test/`, unrelated doc edits.

## Topic contracts

| Topic | Retained | Payload | When published |
|-------|----------|---------|----------------|
| `TASTYTRADE/<symbol>` | Yes | Scalar mid string | Genuine DXLink quote/trade only (+ optional legacy replay) |
| `TASTYTRADE/<symbol>__META` | Yes | JSON provenance | With each genuine scalar (and legacy replay meta) |
| `TASTYTRADE/__SESSION` | Yes | `{stream_session_id, started_epoch, event_kind, symbols_with_quotes}` | Streamer startup |
| `TASTYTRADE/__HEARTBEAT` | Yes | `{stream_session_id, published_epoch, event_kind, symbols_with_quotes}` | Every 5s (configurable via `MQTT_HEARTBEAT_INTERVAL_SEC`) |

### `__META` fields (per symbol)

- `source_event_epoch` — upstream DXLink event time (or callback receipt for real events)
- `published_epoch` — wall time when streamer published
- `stream_session_id` — current streamer session
- `subscription_epoch` — first subscribe time for symbol in session
- `sequence` — monotonic per-symbol counter within session
- `event_kind` — `dxlink_quote`, `dxlink_trade`, or `replay`

## Compatibility behavior

| API / consumer | Behavior after Phase 3 |
|----------------|------------------------|
| `get(symbol)` / `get_market_mid()` | Unchanged — latest retained scalar; staleness gate on connection `_last_msg_at` |
| `get_quote()` | **New** — returns `QuoteSnapshot`; freshness from `source_event_epoch`; strict session + pre-subscription validation |
| Scalar-only subscribers (dashboard, etc.) | Still receive `TASTYTRADE/<symbol>` on genuine events |
| Tick listeners / recorder | Fire on genuine events with `source_event_epoch`; **not** on heartbeat or replay |
| `set_override()` | Unchanged for breach simulation |
| Rollback `TT_LEGACY_REPUBLISH_LAST_MIDS=false` (default) | No periodic scalar republish |

## Message-count impact

**Removed (default path):** unconditional every-5s republish of all cached mids (`if tick % 5 == 0: for sym, mid in last_mids`).

Example with 231 symbols carrying mids:

| Path | Approx. MQTT publishes / 5s |
|------|----------------------------|
| **Before** | 231 scalar republishes (+ health write) |
| **After (default)** | 1 `__HEARTBEAT` only |
| **Per genuine tick** | +1 scalar +1 `__META` (was scalar only) |

Net effect: large reduction in synthetic traffic (~231 → 0 scalars per 5s interval). Genuine-event traffic increases by one `__META` per price update. Recorder poll rows (`GLD_polls.csv`, etc.) drop proportionally because synthetic ticks no longer fire tick listeners.

**Rollback (`TT_LEGACY_REPUBLISH_LAST_MIDS=true`):** periodic scalar + replay `__META` restored for emergency compatibility; replay does not advance `source_event_epoch`, sequence, tick listeners, or `get_quote()` eligibility.

## Test results

### Focused MQTT / streamer

```
uv run pytest tests/test_mqtt_source_provenance_phase3.py \
  tests/test_mqtt_prices_resilience.py tests/test_streamer_stale.py -q
→ 29 passed
```

### Full non-integration suite

```
uv run pytest tests/ -q --ignore=tests/integration
→ 434 passed
```

### Required Phase 3 scenarios covered

| Scenario | Test |
|----------|------|
| Real update advances source timestamp + sequence | `test_genuine_update_advances_source_timestamp_and_sequence` |
| Heartbeat does not advance symbol freshness | `test_heartbeat_does_not_advance_symbol_freshness` |
| Legacy republish does not advance source freshness | `test_legacy_republish_does_not_advance_source_freshness` |
| Old-session quote rejected | `test_retained_old_session_quote_rejected` |
| Current-session quote accepted | `test_current_session_quote_accepted` |
| Pre-subscription quote rejected | `test_pre_subscription_quote_rejected_in_strict_validation` |
| Scalar-only legacy consumers still get prices | `test_scalar_only_legacy_consumers_still_receive_prices` |
| Replay does not notify tick listeners | `test_replay_scalar_does_not_notify_tick_listeners` |
| Streamer restart → new session ID | `test_streamer_restart_generates_new_session_id` |
| Meta before/after scalar safe | `test_metadata_before_and_after_scalar_handled_safely` |

## Out of scope (deferred)

- Fill-time breach grace
- Consecutive breach confirmation
- MQTT entry fallback
- Adaptive stop reconciliation
- Dashboard badges
