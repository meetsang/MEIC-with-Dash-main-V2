# REST Pile-Up Fix — Fill Sync, Entry Fallback, Stop Reconcile

**Status:** Design only (no code changes yet).  
**Triggered by:** [LIVE_SESSION_2026-07-09.md](LIVE_SESSION_2026-07-09.md) — 429 cooldown @ 13:44 CT, 01-15_P missing long fill, 01-45 scan failure.  
**Operator grooming:** 2026-07-09 evening session.

---

## Problem statement

On Jul 9, TastyTrade returned **`429 Too Many Requests`** and the bot entered a **5-minute REST cooldown** (`common/broker_cooldown.py`, `runtime/broker_cooldown.json`). During cooldown, the **01-45** entry window failed (0/67 quotes). Earlier, **01-15_P** never received a brokerage long-leg fill in JSON and had **no exchange stop** — but fill sync kept polling REST every **3 seconds** for hours.

**Operator attribution:** primary REST waste was **01-15_P fill sync** plus **dashboard fill sync on laptop and phone** (two summary pollers ~every 3s). Entry scans were a smaller share of total REST volume.

This document specifies four coordinated changes:

| ID | Change |
|----|--------|
| **F1** | Fill sync: tri-value inference (short / long / spread) + phased polling + backoff |
| **F2** | Single fill-sync owner: launcher only; stop monitor consumes |
| **F3** | Entry scan: MQTT cache fallback during REST cooldown |
| **F4** | Stop reconcile: adaptive REST interval (slow when open, fast when closing) |

---

## Current architecture (code map)

### Processes that hit TastyTrade REST today

```
run.py (launcher)          every 5s loop  → sync_pending_fills()
blocks/stop/run.py         scan + V3      → sync_pending_fills() every 10s
dashboard/server.py        each summary   → maybe_sync_active_trades() ~3s
dashboard/manual_spread    on manual act  → sync_pending_fills()
```

Each process has its **own** `get_shared_broker()` instance (`common/broker_factory.py` — keyed by PID). The **2s live-orders cache** (`TT_LIVE_ORDERS_CACHE_TTL_SEC`) is **per process**, not shared. Three pollers ⇒ up to **3×** `get_order_status` / `get_live_orders` for the same `open_order_id`.

### Fill sync path

| File | Role |
|------|------|
| `blocks/stop/fill_sync.py` | `sync_open_order()`, `apply_order_result_to_state()`, intervals |
| `blocks/stop/pending_fill_sync.py` | `needs_open_order_sync()`, `sync_pending_fills()` over all active JSONs |
| `brokers/tastytrade_broker.py` | `_order_result_from_placed_order()` → `short_fill_price`, `long_fill_price`, `filled_price` (spread credit) |
| `brokers/tastytrade_broker.py` | `get_order_status()` → `get_live_orders_cached()` then `get_order()` fallback |

**Handshake today:** entry worker writes `open_order_id` + initial JSON → fill sync promotes to `status=open` when both leg prices > 0 → stop monitor starts when `open` + full qty.

**Gap today:** `apply_order_result_to_state()` only writes leg prices when broker returns them (`fill_sync.py:52–57`). No inference. When `fully_filled=true` but one leg is 0, `sync_open_order()` keeps polling at **3s** (`fill_sync.py:111–115`, `PENDING_FILL_SYNC_INTERVAL_SEC`).

### Entry scan path

| File | Role |
|------|------|
| `blocks/entry/config.py` | `quote_source: str = 'api'` (default) |
| `blocks/entry/spread_scan.py` | `scan_credit_spreads()`, `_fetch_option_mids_robust()`, `_resolve_spx_price()` |
| `blocks/entry/meic_worker.py` | `SCAN_PICK_RETRIES = 3` per side |

- **`quote_source='api'`:** REST batches for option mids; SPX REST with **MQTT fallback** (`_resolve_spx_price:309–315`).
- **`quote_source='mqtt'`:** register symbols + `broker.get_option_price()` from live cache — **not used by default** and **not auto-selected during cooldown**.

### Stop monitor path

| File | Role |
|------|------|
| `blocks/stop/monitor.py` | `SLOW_INTERVAL = 10` — REST stop reconcile; breach via MQTT |
| `blocks/stop/v3/supervisor.py` | `TARGET_CYCLE_SEC = 0.25` — fast MQTT breach loop; `_slow_broker_sync` every 10s |
| `blocks/stop/v3/quotes.py` | MQTT-first leg quotes for closes; REST fallback |

**Breach detection:** MQTT only (`monitor.py:683`, supervisor `_scan_open_slot`).  
**Order status:** REST `get_order_status` every 10s per open trade (`monitor.py:675–679`).

---

## Jul 9 evidence

### 01-15_P (`trades/active/MEIC_IC/01-15_P_20260709T131405.json`)

| Field | Value |
|-------|-------|
| Fill time | ~13:14 CT |
| `short_leg.fill_price` | $1.15 |
| `long_leg.fill_price` | $0.00 |
| `entry.net_credit` | $0.75 |
| `open_order.fully_filled` | `true` |
| Implied long | $1.15 − $0.75 = **$0.40** |

### Cooldown

```json
"reason": "429 Too Many Requests",
"set_at": 1783622645  →  2026-07-09 13:44:05 CT
```

### REST volume estimate (01-15_P fill sync only)

| Window | Launcher polls (@ 3s min) | Notes |
|--------|---------------------------|-------|
| 13:14 → 13:44 (30 min) | ~600 | Cooldown fires |
| 13:14 → 15:00 (106 min) | ~2,100 | Full afternoon |

With dashboard (2 clients @ ~3s) + stop monitor (@ 10s), effective multiplier **~2–3×** while `needs_open_order_sync` is true.

### Entry scan (not the main issue)

- ~**67 symbols** per scan = ~**2** REST `get_market_data_by_type` calls per pass (batches of 40 in `spread_scan`, 100 in broker).
- Jul 9: 4 lots × 2 sides + chase ≈ **100–150** REST market-data calls/day — acceptable alone.

---

## F1 — Fill sync: tri-value inference + phased polling

### Three broker values

For a credit spread **open** (SELL short / BUY long):

| Key | `OrderResult` field | JSON field |
|-----|---------------------|------------|
| Short leg | `short_fill_price` | `short_leg.fill_price` |
| Long leg | `long_fill_price` | `long_leg.fill_price` |
| Spread credit | `filled_price` | `entry.net_credit` |

Identity (credit spread):

```
spread = short − long
long   = short − spread
short  = long + spread
```

Any **two** known positive values determine the third (round to $0.01).

### Operator rule (groomed)

1. **Fast poll (3s):** keep calling `get_order_status` until **≥2 of 3** values are present from broker.
2. **Confirmation poll (one more):** after reaching 2, perform **exactly one** additional REST poll to see if broker sends all 3.
3. **Infer:** if still missing one value after confirmation, **calculate** it from the identity above.
4. **Backoff:** after inference (or after all 3 from broker), stop 3s hammering; use exponential backoff for any optional reconciliation (e.g. 30s → 60s → 120s cap).

### Proposed state machine (per trade JSON)

Add under `open_order` (or `lifecycle.fill_sync`):

```json
{
  "fill_sync_phase": "fast | confirm | inferred | complete",
  "fill_sync_confirm_pending": false,
  "fill_inferred": false,
  "fill_inferred_field": "long_leg.fill_price",
  "fill_sync_backoff_sec": 30,
  "fill_sync_next_poll_epoch": 0
}
```

| Phase | Interval | Exit condition |
|-------|----------|----------------|
| `fast` | 3s | `count_present(short, long, spread) >= 2` |
| `confirm` | immediate (one shot) | Next poll completes |
| `inferred` | — | Compute missing field; set `fill_inferred=true`; log WARNING |
| `complete` | backoff / stop | Both legs > 0; `status=open`; stop monitor can arm |

### Code touch points

| File | Change |
|------|--------|
| `blocks/stop/fill_sync.py` | New `count_fill_fields()`, `infer_missing_fill()`, phase machine in `sync_open_order()` / `apply_order_result_to_state()` |
| `blocks/stop/fill_sync.py` | `fill_sync_interval_sec()` returns backoff when `fill_sync_phase=complete` and inferred |
| `blocks/stop/state.py` | Optional helpers for new `open_order` sub-keys |
| `brokers/tastytrade_broker.py` | No change required — already populates all three when legs parse |
| `tests/test_fill_sync.py` | Cases: short+spread→long; short+long→spread; confirm poll gets 3rd; backoff after infer |
| `tests/test_pending_fill_sync.py` | 01-15_P scenario: infer then `needs_open_order_sync` → false |

### Inference guardrails

- Only infer when `status == 'filled'` (or `fully_filled`) and `filled_quantity >= quantity`.
- Only infer missing field; never overwrite broker-provided values.
- Log: `fill_inferred lot=01-15_P field=long_leg.fill_price value=0.40 from short=1.15 spread=0.75`.
- Persist `fill_inferred: true` on JSON for audit / dashboard badge.

### 01-15_P counterfactual

With this design, by ~13:14:30 (a few fast polls + one confirm), long would be inferred at **$0.40**, stop would arm, and REST would drop from **3s** to **30s+** backoff — avoiding thousands of afternoon polls.

---

## F2 — Single fill-sync owner (launcher)

### Decision

| Role | Owner |
|------|-------|
| Pending open-order fill sync | **`run.py` launcher** only |
| Promote to `open` + register MQTT symbols | Launcher (existing `pending_fill_sync.register_spread_symbols`) |
| Breach / stop / close | **Stop monitor** (consumer) |

### Remove duplicate callers

| File | Action |
|------|--------|
| `run.py` | **Keep** `sync_pending_fills()` in main loop (5s) |
| `blocks/stop/v3/supervisor.py` | **Remove** `_sync_pending_fills()` from `_discover_slots()` |
| `blocks/stop/runner.py` | **Remove** `_sync_pending_fills()` from `_scan_for_new()` |
| `dashboard/server.py` | **Remove** `maybe_sync_active_trades()` from `_read_active_trades()` |
| `dashboard/manual_spread_handlers.py` | **Review** — one-shot `sync_pending_fills(force=True)` after manual place may stay (operator-initiated, not polled) |

### Stop monitor as consumer

Today `MonitorRunner.add()` / V3 `_slot_eligible()` require `status == 'open'` and full fill (`runner.py:146–151`, `supervisor.py:157–168`). No change to eligibility — only ensure launcher has promoted the trade before stop monitor discovers it.

**Discovery timing:** launcher loop 5s + fill sync 3s ⇒ worst case ~8s before monitor sees `open`. Acceptable.

### Dashboard read path

After removal, `_read_active_trades()` reads JSON from disk only. Display may lag fill promotion by one launcher cycle — acceptable vs REST spam.

**Jul 9 impact:** removing dashboard sync on **two clients** eliminates ~**40 polls/min** during 01-15_P stuck state.

### Tests

| File | Change |
|------|--------|
| `tests/test_broker_hardening.py` | Update dashboard fill-sync tests — expect no sync on summary |
| `tests/test_stop_runner_gate.py` | Runner no longer calls `sync_pending_fills` |
| New integration test | Launcher promotes fill; stop monitor picks up without calling sync |

---

## F3 — Entry scan: MQTT fallback during REST cooldown

### Problem

`scan_credit_spreads(..., quote_source='api')` calls `_fetch_option_mids_robust()` → REST only. When `BrokerCooldownActive` or 429, scan gets **0/N quotes** (01-45 @ 13:46).

SPX already falls back to MQTT (`spread_scan.py:309–315`). **Options do not.**

### Proposed behavior

In `_fetch_option_mids_robust()` or `scan_credit_spreads()`:

```
if broker_cooldown.active() OR api_coverage < 50%:
    switch to mqtt path for this scan only
```

MQTT path (existing `quote_source='mqtt'` branch):

1. `_register_scan_symbols()` — add scan symbols to streamer watch.
2. `_leg_prices()` — `broker.get_option_price(symbol, timeout=2.0)` from live cache.

### Freshness gates (operator requirement — avoid 11-00_P stale breach class)

Before accepting an MQTT mid for **entry scan** (not breach):

| Gate | Rule | Config env |
|------|------|------------|
| **Age** | `now - price_ts < MAX_MQTT_QUOTE_AGE_SEC` | default **10** |
| **Subscribed** | symbol in streamer / ladder watch set | existing `register_symbols_and_wait` |
| **Sanity** | `credit_min <= (short − long) <= credit_max * SANITY_MULT` | default **2.0×** band |
| **Both legs** | short and long must pass gates on same tick window | |

Reject scan candidate if any leg fails — do **not** pick strikes on stale MQTT.

**Do not** read `spx_ladder_quotes.csv` (60s sampled history) for entry.

### Code touch points

| File | Change |
|------|--------|
| `blocks/entry/spread_scan.py` | `resolve_quote_source()`, cooldown check, freshness in `_leg_prices` mqtt branch |
| `common/broker_cooldown.py` | `cooldown_active()` already exists |
| `common/mqtt_prices.py` | Expose price age if not already on cache |
| `blocks/entry/config.py` | Optional `mqtt_fallback_on_cooldown: bool = True` |
| `tests/` | Mock cooldown → assert mqtt path; stale price → reject candidate |

### 01-45 counterfactual

If MQTT cache had fresh mids for scan symbols at 13:46, tranche might have fired without REST. If symbols were not subscribed yet, register + wait (existing pattern) — may add **2–5s** latency once per scan, still better than empty scan.

---

## F4 — Adaptive stop reconcile interval

### Problem

Every open trade polls `get_order_status` on active stop every **10s** (`SLOW_INTERVAL`). With 7–8 open trades ≈ **42–48 REST calls/min** for stop reconcile alone.

### Proposed rule

| Trade state | MQTT health | REST reconcile interval |
|-------------|---------------|-------------------------|
| `open` | healthy | **15–20s** (config `STOP_RECONCILE_OPEN_SEC`, default 15) |
| `open` | stale | keep 10s or pause broker actions (existing stale freeze) |
| `closing` / `close_only_mode` | any | **5s** or current 10s — **do not slow** |
| Long chase active | any | **fast** — existing `_poll_spread_close` / long order polls |

**Breach loop:** unchanged — MQTT @ 0.25s (`TARGET_CYCLE_SEC`).

**Long close deadline:** `LONG_CLOSE_DELAY_SEC = 30` is delay **before** long chase **starts** after short fills (`monitor.py:61`). F4 does not change that. F4 only widens **stop working-order** poll when peacefully open.

### Code touch points

| File | Change |
|------|--------|
| `blocks/stop/monitor.py` | `reconcile_interval_sec(state)` replaces fixed `SLOW_INTERVAL` where appropriate |
| `blocks/stop/v3/supervisor.py` | `slot.last_broker_sync` uses adaptive interval |
| `blocks/stop/v3/config.py` | `STOP_RECONCILE_OPEN_SEC`, `STOP_RECONCILE_CLOSING_SEC` |
| `tests/` | open+healthy → 15s; closing → 5s |

### MQTT fill alerts

When `alert_listener` delivers stop fill events (`monitor.py:240–255`), REST reconcile is backup only — widening open interval is safe if alerts are healthy.

---

## Implementation order

| Phase | Items | Risk |
|-------|-------|------|
| **1** | F1 inference + backoff | Fixes 01-15_P class; biggest REST win |
| **2** | F2 single owner | Quick delete; removes dashboard multiplier |
| **3** | F4 adaptive reconcile | Moderate REST savings; low risk if closing stays fast |
| **4** | F3 MQTT entry fallback | Higher complexity; needs freshness tests |

**Do not mix** with P0 breach grace / dashboard overlay fixes in same PR — separate concerns.

---

## Test plan (when implementing)

1. **Unit:** tri-value inference all combinations (short+spread, long+spread, short+long).
2. **Unit:** confirm poll receives 3rd value → no inference.
3. **Unit:** backoff intervals after inference.
4. **Unit:** entry mqtt fallback rejected when price age > 10s.
5. **Unit:** entry mqtt fallback accepted when fresh + in band.
6. **Integration:** single launcher sync promotes trade; stop monitor arms without duplicate sync.
7. **Regression:** `tests/test_fill_sync.py`, `tests/test_pending_fill_sync.py`, `tests/test_broker_hardening.py`.

---

## Open questions

| # | Question | Default if unanswered |
|---|----------|------------------------|
| Q1 | Backoff cap after inference (30s? 60s? stop after infer?) | 60s cap; stop polling open order once inferred + both legs set |
| Q2 | Dashboard show `fill_inferred` badge? | Yes — small badge on tranche row |
| Q3 | Manual spread handler keep `sync_pending_fills(force=True)`? | Yes — one-shot after place |
| Q4 | `STOP_RECONCILE_OPEN_SEC` default 15 or 20? | **15** |

---

## Related docs

- [LIVE_SESSION_2026-07-09.md](LIVE_SESSION_2026-07-09.md) — incident narrative
- [STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md](STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md) — MQTT cache patterns
- [MARKET_DATA_EXPANDED_WATCH_DESIGN.md](MARKET_DATA_EXPANDED_WATCH_DESIGN.md) — sidecar ladder / live cache
