# Live Session Notes — Jul 9, 2026

**Status:** Session complete (EOD notes).  
**Related:** [LIVE_SESSION_2026-07-08.md](LIVE_SESSION_2026-07-08.md), [STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md](STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md), [MARKET_DATA_EXPANDED_WATCH_DESIGN.md](MARKET_DATA_EXPANDED_WATCH_DESIGN.md)

---

## Day summary

| Tranche | Side | Entry credit | Close | PnL (per lot) | Notes |
|---------|------|-------------|-------|---------------|-------|
| 11-00 | C | $0.70 | exchange_stop @ 11:05 | −$60 | Short stop filled |
| 11-00 | P | $1.05 | **expiry_settlement** | +$105 | False breach at fill; held to expiry OTM |
| 12-00 | C | $1.10 | exchange_stop | −$55 | |
| 12-00 | P | $1.15 | **expiry_settlement** | +$115 | Phase-2 stop upgraded 14:28; settled OTM |
| 12-30 | C | $0.75 | **expiry_settlement** | +$75 | Phase-2 stop; settled OTM |
| 12-30 | P | $1.10 | software_breach @ 14:08 | −$135 | Real breach ~2h after fill |
| 01-15 | C | $0.85 | **expiry_settlement** | +$85 | Settled OTM |
| 01-15 | P | $0.75 | **expiry_settlement** | +$75 | **No exchange stop placed** (long fill never synced) |
| 01-45 | — | — | — | — | **Tranche did not fire** (broker cooldown) |
| 02-00 | — | — | — | — | **Did not fire** — operator manually paused in morning (expected) |

**SPX settlement (3 PM CT):** 7542.895 (`data/2026-07-09/spx_mqtt_settlement.json`)

---

## 1. Option prices out of sync (dashboard / breach column)

### Operator symptom

Put spreads showed **2× breach** on the grid even when positions were profitable. Option marks and breach gaps looked wrong for much of the day.

### Root cause (stacked — not streamer overload)

| Layer | What happened |
|-------|----------------|
| **A. False breach at fill** | **11-00_P** breached **1 second after fill** (10:59:21→22). MQTT spread_mid was **$2.90** vs threshold **$2.30** while entry credit was only **$1.05**. Real spread was ~$1.00 within minutes. Classic **stale/pre-subscription MQTT** at the instant of fill. |
| **B. Frozen `breach_watch`** | After breach, trade entered `close_only_mode` with an active exit job. V3 supervisor returns early when `exit_pool.has_job()` — **`_refresh_breach_watch` never runs again**. Snapshot stuck at `updated_at: 10:59:21` all day. |
| **C. Dashboard display bug** | `dashboard/server.py` derives `cur_short` from stale `breach_watch.spread_mid` when watch says both legs had MQTT — even if live MQTT is available. Breach column can show live gap, but **PnL/spread column uses frozen snapshot**. |
| **D. Afternoon streamer faults** | Streamer exited at **14:29** and **14:38** (launcher restarted). **12-00_P** breach checks frozen 12:15–12:16 (`streamer stale >30s`). Not the main morning issue but added afternoon noise. |

### What it was *not*

- Streamer was **not overloaded**: ~231 symbols subscribed (cap 500), health `live` most of the day.
- MQTT cache health showed connected with 200+ prices by mid-morning.
- `options_quotes.csv` is a **3-minute snapshot** of trade legs only — not a live per-tick feed.

### Evidence files

- `trades/active/MEIC_IC/11-00_P_20260709T105904.json` — `breach_watch.updated_at` frozen at fill; `close_only_mode: true`
- `meic0dte/logs/stop_monitor.log` — exit-job spam from 10:59:21 onward
- `data/2026-07-09/options_quotes.csv` — 11-00 P legs at 11:41: short $1.725 / long $0.725 → spread **$1.00** (healthy)

### Fix candidates (deferred)

1. Refresh `breach_watch` even during active exit jobs (display-only).
2. Dashboard: never derive `cur_short` from stale watch when live MQTT exists.
3. Fill-time grace period before software breach can fire (e.g. 5–10s after fill + both legs subscribed).

---

## 2. SPX / PCS out of sync ~11:20–11:30 AM (operator correction)

### Operator symptom (revised)

**Not** the 08:36 cold-start gap, and **not** exactly at 11:00. Around **11:20–11:30 AM**, with **11-00_C already stopped** (~11:05), **PCS marks and SPX** on the dashboard looked out of sync with the broker.

### What the data shows (11:20–11:39 CT)

| Source | Finding |
|--------|---------|
| `SPX_polls.csv` | **1,413 ticks**, **0 gaps >30s**, range **7524.2 – 7529.4** |
| `SPX_1m.csv` | 1-minute bar closes track polls within ~1 min (e.g. `11:25` close `7525.6`) |
| `mqtt_cache_health.json` | Connected, 251 prices, `stale: false` end of day |
| `options_quotes.csv` | 180s snapshots — at `11:20` 11-00 P legs still show inflated spread (P7495 **$2.85** / P7470 **$1.08** → ~$1.77 vs ~$1.00 healthy later) |

**SPX MQTT was healthy at 11:20–11:30.** No evidence of a multi-minute SPX stream failure in this window.

### What actually caused the “out of sync” feeling

The operator was seeing **two different data paths on one screen**, now **~20–30 minutes after** the false breach:

| Dashboard area | Data source | What it showed |
|----------------|-------------|----------------|
| **SPX header** | Live MQTT (`live_prices` / index topic) | ~7524–7529 ✓ |
| **PCS spread / PnL / breach** | Frozen `breach_watch` from false breach on **11-00_P** at `10:59:21` | Spread **$2.90**, 2× breach, wrong PnL |

**11-00_C** was already **closed** (exchange stop @ 11:05). The grid mixed a **closed call** with a **put still open** in `close_only_mode` from the false breach — so the PCS side of the picture looked especially wrong while SPX header looked fine.

**Contributing factors:**

1. **11-00_P false breach** at `10:59:21` froze `breach_watch` for the rest of the day (see §6).
2. **Dashboard overlay** (`dashboard/server.py` ~419–427): derives `cur_short` from frozen `spread_mid` instead of live leg marks.
3. **Ladder / options_quotes** at 11:20 still showed **elevated put mids** (settling after open volatility) — reinforces wrong PCS display even though true spread was recovering.
4. **Quote-type mismatch** (minor for SPX): streamer uses bid/ask **mid**; broker UI often shows **last trade** — a few SPX points is normal and does not explain the PCS breach column.

### Conclusion

“SPX out of sync at 11:20–11:30” was still **primarily a display-layer mismatch**: live SPX header vs frozen PCS overlay from the 10:59 false breach, with CCS already stopped. **Not** an SPX stream outage. Settlement captured correctly at 15:00: **7542.895**.

---

## 3. GLD_polls (~29K) vs spx_ladder_quotes (~75K) — sampling vs tick dump

### Short answer

**No meaningful gap during the session.** The row counts measure different things.

| File | Rows | What each row is | Expected rate |
|------|------|------------------|---------------|
| `GLD_polls.csv` | **28,822** | **1 tick** per MQTT message (~1/sec) | ~6.4h × 3600 ≈ 23K–29K ✓ |
| `spx_ladder_quotes.csv` | **75,423** | **1 option strike** per 60s snapshot | 383 snapshots × ~197 strikes ≈ 75K ✓ |
| `SPX_polls.csv` | **24,460** | Same as GLD (includes overnight header rows) | ✓ |
| `options_quotes.csv` | **1,113** | Trade-leg snapshots every **180s** | 127 snaps × ~9 legs ✓ |

### Operator question: why sample once per minute instead of dumping MQTT as-is?

**Correct — ladder is sampled, not tick-dumped.**

| | GLD / SPX polls | Ladder today |
|--|-----------------|--------------|
| **Trigger** | Every MQTT tick on one symbol | Every **60s** (`SPX_LADDER_REFRESH_SEC`) |
| **Rows** | ~1 per second per symbol | ~197 strikes × 1 row each per snapshot |
| **Code** | `market_data/aggregator.py` → `record_tick()` | `market_data/spx_ladder_snapshots.py` → `maybe_write()` reads cache mids at snapshot time |
| **Rows/day (rough)** | ~25K (1 symbol) | ~75K (197 strikes × 60s) |
| **Tick dump equivalent** | N/A | **~12–40M rows** (197 strikes × ~1 tick/s × 6.4h) |

The **live MQTT cache** for ladder symbols updates every tick (`market_data/spx_ladder.py` + streamer). Only the **CSV writer** samples at 60s. Research/history files trade disk space for a manageable grid.

### Operator decision: keep 60s sampling

**Leave ladder at 1-minute snapshots for now.** The live MQTT cache still updates every tick for trading and monitoring; only the research CSV is sampled. Tick-dump mode is deferred unless disk/query needs change later.

### Actual gaps found

| Gap | Duration | Impact |
|-----|----------|--------|
| **Cold start** | First snapshot `08:36:40` had only **13 strikes** (vs ~200 later) | Ladder/MQTT warming up at session open |
| **Between snapshots** | **0 gaps >90s** for the rest of the day | Sidecar ladder healthy |
| **GLD_polls** | 13 gaps >5s, **0 gaps >60s** | Normal MQTT jitter |

---

## 4. Expired positions show as “Closed” — operator prefers “Expired”

### What happened at 3 PM

Five positions held into settlement were marked in JSON as:

```json
"status": "closed",
"close_mechanism": "expiry_settlement",
"settled_at_expiry": true
```

| Tranche | Settled OTM |
|---------|-------------|
| 11-00_P | ✓ full credit kept |
| 12-00_P | ✓ |
| 12-30_C | ✓ |
| 01-15_C | ✓ |
| 01-15_P | ✓ |

### Why dashboard says “Closed”

`_slot_state_from_trade()` in `dashboard/server.py` only special-cases **kill** and **breach** mechanisms. Everything else with `status: closed` maps to display state **`closed`** → label **“Closed”**.

There is **no** `expired` display state today even though `close_mechanism` is `expiry_settlement`.

### Operator preference

Show **“Expired”** (grey dot, distinct from exchange/breach closes) when `close_mechanism == 'expiry_settlement'` or `settled_at_expiry == true`.

---

## 5. Missing brokerage stop on put side (01-15_P) — deep dive

### Trade: **01-15_P** (7530/7505, $0.75 credit)

| Field | Value |
|-------|-------|
| `short_leg.fill_price` | **$1.15** |
| `long_leg.fill_price` | **$0.00** ← never synced |
| `open_order.fully_filled` | `true` |
| `open_order_id` | `482348463` |
| `lifecycle.breach_arm_status` | **`waiting_stop`** all afternoon |
| `stop_history` | Only `expiry_settlement` at 15:00 — **no `placed` stop** |
| `active_stop` | `null` |

Contrast: **01-15_C** (same tranche, 5s later) synced both legs (`0.95` / `0.10`) and placed stop `482348507`.

### Operator question: if the order shows filled with short price, why not back-calculate the long?

**Today the bot does not infer missing legs.** It only writes prices the brokerage API returns per leg.

**Fill sync path** (`blocks/stop/fill_sync.py`):

1. Stop monitor polls `broker.get_order_status(open_order_id)` every ~3s.
2. TastyTrade response is parsed in `brokers/tastytrade_broker.py` — `SELL_TO_OPEN` → `short_fill_price`, `BUY_TO_OPEN` → `long_fill_price`.
3. Each leg is written **only if the API returns that leg’s fill**:

```52:57:blocks/stop/fill_sync.py
    short_fill = getattr(result, 'short_fill_price', None)
    long_fill = getattr(result, 'long_fill_price', None)
    if short_fill is not None:
        state['short_leg']['fill_price'] = round(float(short_fill), 2)
    if long_fill is not None:
        state['long_leg']['fill_price'] = round(float(long_fill), 2)
```

4. Stop placement requires **both** legs > 0 (`blocks/stop/monitor.py` ~359):

```python
if short_fill <= 0 or long_fill <= 0:
    return  # no exchange stop placed
```

**What the bot was seeking from brokerage:** per-leg fill prices on order `482348463`, not a spread-level guess. The order was marked `fully_filled=true` with short `$1.15`, but TastyTrade **never returned `long_fill_price`** in subsequent polls — so sync kept running (`sync_open_order` continues when `fully_filled` but `long_px=0`) and stop arm stayed blocked.

**Back-calculation fallback (not implemented):**

If `status=filled`, `net_credit` known, and short fill known but long missing:

```
implied_long = short_fill − net_credit = 1.15 − 0.75 = $0.40
```

A safe fallback would: infer from `short − credit`, log `fill_inferred_long`, then arm stop. Would have protected this tranche at the exchange.

Operator observation matches: **no working stop at brokerage** for this put tranche. Market was uptrending; position expired OTM so no harm today — but tranche was **unprotected at the exchange**.

---

## 6. 11-00_P false breach — what went wrong (operator follow-up)

### Timeline (CT)

| Time | Event |
|------|-------|
| `10:59:10` | 11-00_C filled ($0.70 credit) |
| `10:59:11` | 11-00_P entry placed |
| `10:59:21` | P spread filled ($1.05); sync complete |
| `10:59:21` | Exchange stop **placed** `482267049` @ **$3.50** |
| `10:59:21` | `breach_watch`: spread **$2.90**, threshold **$2.30**, status **breached** |
| `10:59:22` | Stop **cancelled** — `breach_cancel:spread_stop_breach` |
| `10:59:22` | Limit close **placed** `482267062` @ **$4.70** — never filled |
| `11:05:08` | 11-00_C exchange stop filled (separate side) |
| Rest of day | 11-00_P held OTM; settled +$105 |

### Mechanism — why wrong breach fired

**Phase 1 software breach** (`blocks/stop/phases.py`):

- Reads short/long MQTT mids → `spread_mark_price` → compares to `current_stop_price()`.
- Threshold = 2× net credit + $0.20 = **$2.30** for $1.05 credit.
- At `10:59:21`, MQTT reported spread **$2.90** while true fill spread was **$1.05** (short $1.85, long $0.80).
- `streamer_stale: false`, `mqtt_cache_stale: false` — health flags did **not** block; **stale leg prices inside a nominally live cache** slipped through.
- **No fill-time grace period** exists — breach can fire the same second as fill.

**Why stop cancelled 1s later:** `replace_with_limit_close()` cancels the exchange stop and places a short-leg limit close at live MQTT short mid (~$4.70). `stop_history` sequence: placed → cancelled → replaced_limit, all within 1 second.

**We cannot afford wrong breaches on stale prices.** Fix backlog item #1 (P0): fill-time grace (5–10s after fill + both legs subscribed) + reject breach when spread_mid diverges from entry credit by more than N×.

---

## 7. 01-45 tranche did not fire — plain English

### What happened (fifth-grader version)

At **1:45 PM**, the bot tried to find a good options trade. To do that it needs to **ask the brokerage “what do these options cost?”** over and over.

But earlier, the brokerage had said **“slow down — you’re asking too fast.”** The bot flipped a **5-minute cooldown switch** (`common/broker_cooldown.py`, default **300 seconds**) that blocks those questions.

During the **01-45 entry window (13:44–13:50 CT)**, every price question was answered with **“nope, cooldown.”** The bot got **0 prices out of 67 options** it needed.

With no prices, it couldn’t find any spread in the credit band → **“no in-band credit (empty scan)”** → tranche **did not fire**. No `01-45_*_20260709*.json` was created.

### Evidence

Launcher terminal (~13:45–13:47 CT):

```
Broker call failed: cooldown active — skipped broker_call
fetch_option_mids_api batch failed: cooldown active
Low quote coverage (0/67) — scan may skew toward strikes with REST data
Scan pick failed: 01-45 C: no in-band credit (empty scan)
Scan pick failed: 01-45 P: no in-band credit (empty scan)
Entry terminated: 01-45 P: no in-band credit (empty scan)
```

`meic0dte/logs/01-45_*.log` — still Jun 25 only (not updated Jul 9).

### Root cause chain (technical)

1. **Broker cooldown** blocked TastyTrade REST calls during the 01-45 window.
2. Entry scan got **0/67 option quotes** → empty credit scan.
3. Both **01-45 C** and **01-45 P** failed pick after retries.
4. `RuntimeWarning: coroutine 'get_market_data_by_type' was never awaited` at `spread_scan.py:302` is a **misleading stack line** (points at a `yield` in `_iter_spread_legs`). The real failure is REST cooldown; MQTT fallback for SPX worked but **not for option scan** (see below).

### Operator question: is cooldown because we're streaming too much?

**Partly related, but different channels.**

| Channel | What happened Jul 9 |
|---------|---------------------|
| **MQTT streamer** | Separate websocket path — kept running. At 13:46:27 SPX **MQTT fallback succeeded** when REST was blocked. |
| **REST API** | TastyTrade returned **`429 Too Many Requests`** (nginx). `runtime/broker_cooldown.json` records this. Cooldown = **5 minutes** blocking NORMAL/LOW REST calls. |

So the throttle was **HTTP REST rate limiting**, not “too many MQTT subscriptions.” Streaming ~231 symbols did not directly trigger the 429.

**What piled up REST calls:** entry scans (`fetch_option_mids_api` in batches of 40), fill sync (`get_order_status` every ~3s per trade), stop monitor order polls, live orders cache, etc. — **multiple processes sharing one broker REST budget** (`common/rest_limiter.py` caps ~1 req/s per process, but aggregate still hit TT’s limit).

### Operator question: use streamer / ladder for entry scan fallback?

**Yes — but only live cache with freshness gates, not stale data.**

Today MEIC entry defaults to `quote_source='api'` (`blocks/entry/config.py`):

| Step | REST (`api`, default) | MQTT (`mqtt`, legacy) |
|------|-------------------------|------------------------|
| SPX price | REST; **MQTT fallback** if REST empty | MQTT only |
| Option mids for scan | REST batches via `_fetch_option_mids_robust` | Register symbols → `broker.get_option_price()` from **live cache** |
| During cooldown | **All REST blocked** → **0/67 quotes** | Would still read cache (if symbols subscribed) |

At 01-45, REST was in cooldown → scan got **0/67** even though streamer had live prices. SPX fell back to MQTT; **option scan did not.**

**Recommended direction (backlog #5):**

1. On REST cooldown or low coverage, **auto-fallback to live MQTT cache** for scan legs (reuse `quote_source='mqtt'` path or hybrid).
2. **Freshness gates** (operator concern about stale data is valid — see 11-00_P false breach):
   - Require price age **< N seconds** (e.g. 5–10s).
   - Require symbol **actively subscribed** in streamer.
   - Reject spread if **> N× entry credit band** sanity check before pick.
3. **Do not** use the 60s `spx_ladder_quotes.csv` file for entry — that is sampled history, not live trading input.

The sidecar ladder already maintains a **live** ~197-strike cache updated per MQTT tick; entry should consume that cache during REST outages, with the same anti-stale rules we need for breach detection.

### 02-00

Operator **manually paused** this slot in the morning — **expected**, not an incident.

---

## 8. Session plan UI (fixed same day)

| Change | Detail |
|--------|--------|
| Window times editable | Failed/skipped rows can edit window; resets `state` → `pending` so tranche can refire |
| **Apply on all** | Master row copies credits/qty/width/stop/chase locally to editable rows |
| **Save All** | Top button persists all row edits to today’s session CSV |
| Per-lot header rows | Removed per operator request — edit P/C windows on each row |

Files: `dashboard/server.py`, `dashboard/templates/index.html`, `blocks/entry/runner.py` (clears `_fired` on reset).

---

## Incident timeline (CT)

| Time | Event |
|------|-------|
| **08:36** | Market data recorder starts; SPX/GLD polls resume; ladder first snap (13 strikes) |
| **10:59** | 11-00 IC fills; **11-00_P false breach** within 1s; breach_watch frozen for rest of day |
| **11:05** | 11-00_C exchange stop filled |
| **11:59** | 12-00 IC fills; stops placed on both sides |
| **12:15** | Streamer stale ~30s — 12-00_P breach checks frozen briefly |
| **12:29** | 12-30 IC fills |
| **13:14** | 01-15 IC fills; **01-15_P long fill never syncs → no stop** |
| **13:46–13:47** | **01-45 scan fails** — broker cooldown, 0 quotes, entry terminated |
| **14:08** | 12-30_P real software breach + close |
| **14:28** | 12-00_P phase-2 stop upgrade |
| **14:29 / 14:38** | Streamer exit code 1 — launcher restart |
| **15:00** | Expiry settlement on 5 OTM positions @ SPX 7542.895 |
| **15:30** | 11-00_P final settlement pass (was still `close_only_mode`) |

---

## Open items / fix backlog

| # | Item | Priority |
|---|------|----------|
| 1 | Fill-time breach grace period + stale MQTT guard | P0 |
| 2 | Keep `breach_watch` fresh during exit jobs; fix dashboard stale spread overlay | P0 |
| 3 | Dashboard **Expired** status for `expiry_settlement` | P1 (operator request) |
| 4 | 01-15_P long-fill sync — infer long from `short − credit` when broker omits leg | P1 |
| 5 | 01-45: MQTT cache fallback for entry scan during REST cooldown + freshness gates | P1 |
| 6 | ~~`spx_ladder_ticks.csv` tick-dump~~ — **deferred**; keep 60s sampling per operator | — |
| 7 | Session plan window times + Save All / Apply on all UI | **Done** 2026-07-09 |

---

## Quick reference — where to look

| Question | File |
|----------|------|
| False breach trade | `trades/active/MEIC_IC/11-00_P_20260709T105904.json` |
| Missing stop trade | `trades/active/MEIC_IC/01-15_P_20260709T131405.json` |
| 01-45 failure | Terminal launcher log ~13:46 CT; `blocks/entry/spread_scan.py` |
| SPX health ~11:20–11:30 | `data/2026-07-09/SPX_polls.csv`, `SPX_1m.csv` |
| REST cooldown evidence | `runtime/broker_cooldown.json` (429 Too Many Requests) |
| Ladder sampling | `market_data/spx_ladder_snapshots.py`, `common/market_watch.py` |
| Fill sync / stop arm | `blocks/stop/fill_sync.py`, `blocks/stop/monitor.py` |
| SPX settlement | `data/2026-07-09/spx_mqtt_settlement.json` |
| Poll / ladder health | `data/2026-07-09/SPX_polls.csv`, `spx_ladder_quotes.csv`, `GLD_polls.csv` |
