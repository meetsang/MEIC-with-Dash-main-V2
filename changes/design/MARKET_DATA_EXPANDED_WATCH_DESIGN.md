# Expanded market watch — indices, volume (OHLCV), TLT/GLD, SPX 0DTE ladder

**Status:** implemented (2026-07-09)  
**Goal:** Record **all requested tickers** on the same cadence as SPX/QQQ, add **volume** where meaningful, and stream a **rolling SPX 0DTE options ladder** without breaking MEIC/Manual trade flows.

**Related:** [CHANGES_SINCE_2026-07-06.md](CHANGES_SINCE_2026-07-06.md) (tick-on-arrival OHLC, option snapshots)

---

## Operator request (summary)

| # | Item | Notes |
|---|------|-------|
| 1 | **VIX** | Same tick/OHLC cadence as SPX/QQQ |
| 2 | **Volume** | All symbols **except SPX** → bars become **OHLCV** (not OHLC) |
| 3 | **VXN** | Same as VIX |
| 4 | **TLT** | New equity watch |
| 5 | **GLD** | New equity watch |
| 6 | **SPX 0DTE options ladder** | ±50 strikes × $5 around nearest $5 to SPX; **Calls + Puts**; **mid + volume**; refresh symbol set **every 1 min** from live SPX; feed streamer |

**Constraints**

- Must **not** break MEIC/Manual entry, stop monitor, or existing `optsymbols.json` registration.
- Trade legs and ladder symbols may overlap — streamer **dedupes** subscribe set (already does `set()` union).
- Implementation follows this doc; update `CHANGES_SINCE_2026-07-06.md` when shipped.

---

## Current state (why some tickers are missing)

### Watch list exists in code but data files are empty

`market_data/config.py` and `streaming/publish_tastytrade.py` already list:

```text
SPX, VIX, VXN, QQQ, IWM
```

On **2026-07-08**, `data/2026-07-08/` had `SPX_*`, `QQQ_*`, `IWM_*` but **no `VIX_*` or `VXN_*` files**.

### Root cause: wrong DXLink subscribe symbols for indices

| Canonical (MQTT) | Streamer subscribes today | DXLink likely needs |
|------------------|---------------------------|---------------------|
| SPX | `SPX` + **Trade** channel | `$SPX` / Trade (works) |
| VIX | `VIX` Quote only | **`$VIX`** (or `.$VIX`) Quote |
| VXN | `VXN` Quote only | **`$VXN`** Quote |
| QQQ, IWM | plain equity tickers | plain tickers (works) |

Streamer maps `$VIX` → MQTT topic `VIX` via `_TOPIC_ALIASES`, but **never subscribes** to `$VIX`, so no quotes arrive and market_data never writes files.

### Volume not implemented anywhere

- MQTT payload today: **single float** mid per topic (`TASTYTRADE/QQQ` → `"707.25"`).
- `Quote` events have bid/ask only (no day volume).
- `Trade` events have `size` (print size) and `day_volume` (cumulative).
- Aggregator builds **OHLC** only; no volume column.

### Options today

| Source | Mechanism | Output |
|--------|-----------|--------|
| MEIC / Manual spreads | `optsymbols.json` → streamer | MQTT mids → stop monitor |
| Option snapshots | `load_registered_option_symbols()` every **3 min** | `options_quotes.csv` (mid only) |

No rolling SPX ladder; no option volume.

---

## Target watch universe

### Single source of truth

Introduce **`common/market_watch.py`** (or extend `market_data/config.py` + import from streamer):

```python
WATCH_SYMBOLS = (
    'SPX',   # OHLC only — index, not directly tradeable
    'VIX', 'VXN',           # indices — OHLCV if Trade volume available; else OHLC + day_volume when present
    'QQQ', 'IWM', 'TLT', 'GLD',  # equities — OHLCV from Trade prints
)
```

Remove duplicate tuples in `publish_tastytrade.py` / `watch_symbols.py`; both import the shared list.

### DXLink subscribe map

| MQTT / recorder symbol | DXLink Quote subscribe | DXLink Trade subscribe | Volume strategy |
|------------------------|------------------------|----------------------|-----------------|
| SPX | `$SPX` or `SPX` | **Yes** (already) | **None** (operator request) |
| VIX | `$VIX` | Try `$VIX` if quotes lack volume | Trade `size` sum per bar; else 0 |
| VXN | `$VXN` | Try `$VXN` | Same |
| QQQ, IWM, TLT, GLD | ticker | **Yes** | Sum `Trade.size` per 1m bar |

---

## Architecture (high level)

```text
                    ┌─────────────────────────────────────┐
                    │   streaming/publish_tastytrade.py   │
                    │   DXLink Quote + Trade listeners    │
                    └──────────────┬──────────────────────┘
                                   │ MQTT (price + optional vol topics)
                    ┌──────────────▼──────────────────────┐
                    │      common/mqtt_prices.py          │
                    │   tick listeners (price + volume)   │
                    └──────────────┬──────────────────────┘
                                   │
         ┌─────────────────────────┼─────────────────────────┐
         │                         │                         │
         ▼                         ▼                         ▼
 market_data/recorder      stop_monitor (unchanged)    dashboard (unchanged)
 index OHLCV CSVs          spread legs only             optional later
 options_quotes.csv
 spx_ladder_quotes.csv

         ┌─────────────────────────────────────────┐
         │  market_data/spx_ladder.py (new)        │
         │  every 60s: SPX → strikes → JSON      │
         └──────────────────┬──────────────────────┘
                            │ streaming/spx_ladder_symbols.json
                            └────────► streamer union subscribe
```

**MEIC/Manual path unchanged:** `update_options_symbols()` → `optsymbols.json` → streamer poll loop adds symbols.

**Ladder path (new):** separate JSON file so ladder refresh does not rewrite trade symbol file.

---

## Part A — Fix VIX / VXN / add TLT / GLD (price path)

### Streamer changes

**File:** `streaming/publish_tastytrade.py`

1. Import shared `WATCH_SYMBOLS` and a new `dxlink_subscribe_symbol(canonical: str) -> str` helper.
2. Subscribe **Quote** using DXLink names (`$VIX`, `$VXN`, `QQQ`, …).
3. Subscribe **Trade** for all watch symbols **except SPX** (SPX keeps existing Trade → price only; ignore size for SPX volume).
4. Keep `_mqtt_symbol()` aliases so MQTT topics stay canonical (`VIX`, not `$VIX`).

### Market data changes

**Files:** `market_data/watch_symbols.py`, `market_data/recorder.py`

- Extend `_TOPIC_ALIASES` for `TLT`, `GLD`.
- Tick listener already records on arrival — no cadence change needed once MQTT flows.

### Output files (unchanged names, new symbols)

Per day under `data/YYYY-MM-DD/`:

```text
VIX_polls.csv   VIX_1m.csv   VIX_3m.csv  ...
VXN_polls.csv   ...
TLT_polls.csv   ...
GLD_polls.csv   ...
```

Same intervals as SPX: 1, 3, 5, 10, 30, 60 minutes + SMA/EMA columns on close.

---

## Part B — Volume → OHLCV

### Design principle

- **SPX:** remain `open, high, low, close, samples` — **no volume column**.
- **Everything else in watch list:** add **`volume`** column = sum of trade print sizes during the bar window.

### Why Trade.size (not Quote)

TastyTrade `Quote` model fields: bid/ask/size (liquidity at touch, not daily volume).  
`Trade` model fields: `price`, `size`, `day_volume`.

Per-minute bar volume = **Σ Trade.size** for trades with timestamp in that minute (standard intraday bar build).

Fallback if a symbol gets quotes but sparse trades: `volume = 0` for that bar (document in CSV); optional phase-2 use `day_volume` delta.

### MQTT — backward compatible

**Do not** change existing price topic payload (float string) — stop_monitor and hundreds of tests depend on it.

Add **parallel volume topics**:

```text
TASTYTRADE/QQQ        → "707.25"          (unchanged — mid/last)
TASTYTRADE/QQQ__VOL   → "1523400"         (cumulative day_volume snapshot at publish time)
```

For bar aggregation the recorder uses **tick listener metadata**, not only MQTT vol topics:

```python
# TickListener extended (non-breaking: optional 4th arg or separate VolumeListener)
listener(symbol: str, price: float, epoch: float, trade_size: int | None = None)
```

- **Price ticks** from Quote mid updates (current behavior).
- **Volume increments** from Trade events (`size` passed to listener; recorder adds to current minute bucket).

Option symbols: same pattern — ladder snapshot writer sums recent trade sizes or reads latest `day_volume` delta.

### Aggregator changes

**File:** `market_data/aggregator.py`

```python
@dataclass
class OhlcBar:
    ...
    volume: int = 0

    def absorb_trade(self, price: float, size: int) -> None: ...
```

- `record_tick(ts, price)` — price path (unchanged semantics).
- `record_trade(ts, price, size)` — increments volume + updates OHLC if price used from trade.

**CSV header**

| Symbol type | Columns |
|-------------|---------|
| SPX | `datetime,open,high,low,close,samples,sma_*,ema_*` |
| Others | `datetime,open,high,low,close,volume,samples,sma_*,ema_*` |

`ohlc_header(symbol)` returns the right shape.

### Options volume in snapshots

Extend `options_quotes.csv` header:

```text
snapshot_ts,symbol,mid,volume
```

- `volume` = latest cumulative `day_volume` from Trade feed at snapshot time (or sum of prints since last snapshot — document chosen approach in implementation; prefer **day_volume snapshot** for consistency with equities MQTT `__VOL` topic).

---

## Part C — SPX 0DTE options ladder

### Strike grid algorithm

Given SPX index price `P` (from MQTT `SPX` mid):

```python
anchor = round(P / 5) * 5          # e.g. 7533 → 7535
below  = [anchor - 5*i for i in range(1, 51)]   # 7530, 7525, … (50 strikes)
above  = [anchor + 5*i for i in range(0, 50)]   # 7535, 7540, … (50 strikes, includes anchor)
strikes = sorted(set(below + above))             # 100 strikes
```

For each strike `K` and expiry = **today 0DTE** (`central_date()` → `YYMMDD`):

```python
call = build_tastytrade_symbol(expiry, 'C', K)   # .SPXW260709C7535
put  = build_tastytrade_symbol(expiry, 'P', K)   # .SPXW260709P7535
```

**Total:** 100 strikes × 2 = **200 option symbols** (deduped).

### Refresh cadence

Every **60 seconds** during regular session only (8:30 AM–3:00 PM CT, when SPX MQTT is fresh):

1. Read latest SPX from MQTT cache (same cache as recorder).
2. Recompute strike list.
3. Write `streaming/spx_ladder_symbols.json`:

```json
{
  "updated_at": "2026-07-09 09:31:00",
  "anchor_strike": 7535,
  "spx_ref": 7533.2,
  "SYMBOLS": [".SPXW260709C7530", ".SPXW260709P7530", "..."]
}
```

4. Streamer already polls symbol files ~1s — union `ladder ∪ optsymbols ∪ WATCH` and `subscribe(Quote, new)` (existing pattern).

When anchor moves and strikes roll off, **do not unsubscribe** (operator decision) — harmless duplicate MQTT; no prune in v1.

### Preferred: option chain API vs manual strings

| Approach | Pros | Cons |
|----------|------|------|
| **A. `build_tastytrade_symbol()`** | No API call; fast; already used everywhere | Must match TastyTrade streamer symbol rules exactly |
| **B. `get_option_chain(session, 'SPX')` once/min** | Authoritative `streamer_symbol`; handles holidays/width | Heavier API; async in sync recorder |

**Recommendation:** Start with **A** (pure math + `common/symbols.py`) — same path as MEIC entry. Add **B** as fallback validator if streamer rejects symbols (log + skip).

### Ladder data output

New file: `data/YYYY-MM-DD/spx_ladder_quotes.csv`

```text
snapshot_ts,strike,side,symbol,mid,volume
2026-07-09 09:33:00,7535,C,.SPXW260709C7535,1.125,4520
2026-07-09 09:33:00,7535,P,.SPXW260709P7535,0.875,3100
...
```

Snapshot interval: **60 seconds** (aligned with symbol refresh), independent of trade-leg `options_quotes.csv` (3 min).

`options_quotes.csv` **unchanged** for MEIC/Manual legs only.

---

## Part D — Non-breaking guarantees

| Flow | Risk | Mitigation |
|------|------|------------|
| MEIC/Manual `optsymbols.json` | Ladder overwrites trade symbols | **Separate file** `spx_ladder_symbols.json`; `update_options_symbols()` untouched |
| Streamer subscribe set growth | 200+ ladder + scan symbols | `set()` dedupe already in streamer; DXLink handles large subscribe lists (monitor CPU) |
| MQTT price format | JSON break stop_monitor | Keep float string on existing topics; volume on `__VOL` suffix or in-process listener only |
| `load_registered_option_symbols()` | Ladder pollutes trade snapshots | Keep filter — ladder uses dedicated loader `load_ladder_option_symbols()` |
| Pre-market `optsymbols` cleanup | Clears trade symbols only | Do **not** clear ladder file on 3 PM cleanup (or regenerate next morning from SPX) |
| SPX index in ladder JSON | Accidental index subscribe | Ladder writer emits **options only** |

---

## Implementation plan (phased)

### Phase 1 — Watch list fix (P0)

- [ ] `common/market_watch.py` shared config
- [ ] Streamer: `$VIX`, `$VXN`, `TLT`, `GLD` subscribe + MQTT aliases
- [ ] `watch_symbols.py` aliases for TLT/GLD
- [ ] Verify `VIX_1m.csv`, `VXN_1m.csv`, `TLT_1m.csv`, `GLD_1m.csv` appear on live session

### Phase 2 — OHLCV (P1)

- [ ] Trade listener in streamer for non-SPX watch symbols + option symbols
- [ ] Extended tick listener with `trade_size`
- [ ] Aggregator volume bucket + `ohlc_header(symbol)` split
- [ ] Tests: bar volume = sum of trade sizes

### Phase 3 — SPX ladder (P1)

- [ ] `market_data/spx_ladder.py` — strike math + JSON writer (60s)
- [ ] Streamer reads `spx_ladder_symbols.json`
- [ ] `spx_ladder_quotes.csv` snapshot writer (60s, mid + volume)
- [ ] Tests: strike grid from SPX 7533; symbol count 200; dedupe with optsymbols

### Phase 4 — Polish (P2)

- [ ] Dashboard / analysis hooks for ladder CSV
- [ ] `get_option_chain` validation path

---

## Files to touch (implementation checklist)

| File | Change |
|------|--------|
| `common/market_watch.py` | **New** — canonical watch list + DXLink mapping |
| `streaming/publish_tastytrade.py` | Subscribe map, Trade feeds, union ladder symbols |
| `streaming/spx_ladder_symbols.json` | **New** — runtime-generated (gitignore or empty template) |
| `common/mqtt_prices.py` | Optional volume listener / metadata |
| `market_data/watch_symbols.py` | TLT, GLD, aliases |
| `market_data/aggregator.py` | OHLCV bars |
| `market_data/indicators.py` | `ohlc_header(symbol)` |
| `market_data/recorder.py` | Trade-size drain; wire ladder module |
| `market_data/spx_ladder.py` | **New** — strike grid + JSON writer |
| `market_data/spx_ladder_snapshots.py` | **New** — ladder CSV writer |
| `market_data/config.py` | Ladder paths, refresh intervals |
| `common/stream_option_symbols.py` | Optional: `load_ladder_option_symbols()` |
| `.gitignore` | `streaming/spx_ladder_symbols.json` if treated like optsymbols |
| `tests/test_market_data.py` | OHLCV + ladder strike tests |
| `tests/test_spx_ladder.py` | **New** |

**Explicitly not changed in phase 1–3:** `blocks/stop/*`, `meic0dte/app/utilities.update_options_symbols`, breach math, order placement.

---

## Tests

```powershell
uv run pytest tests/test_market_data.py tests/test_spx_ladder.py tests/test_mqtt_prices_resilience.py -q
```

| Test | Asserts |
|------|---------|
| `test_strike_grid_7533` | anchor 7535; 100 strikes; 200 option symbols |
| `test_ohlcv_volume_sums_trades` | 3 trades in minute → volume = sum(sizes) |
| `test_spx_bar_has_no_volume_column` | SPX CSV header lacks `volume` |
| `test_watch_symbol_aliases` | `$VIX` → `VIX` |
| `test_ladder_union_dedupes_optsymbols` | overlap symbol appears once in subscribe set |

---

## Live validation checklist

After deploy + `run.py` restart:

- [ ] `data/YYYY-MM-DD/VIX_1m.csv` growing; `samples` > 2 when streamer healthy
- [ ] `VXN_1m.csv`, `TLT_1m.csv`, `GLD_1m.csv` same
- [ ] QQQ/IWM bars include **volume** column; SPX bars do **not**
- [ ] `streaming/spx_ladder_symbols.json` updates every ~60s; `anchor_strike` tracks SPX
- [ ] `spx_ladder_quotes.csv` rows ≈ 200 per snapshot (fewer if illiquid / no MQTT yet)
- [ ] MEIC entry still adds legs to `optsymbols.json`; stop monitor MQTT unchanged
- [ ] `options_quotes.csv` still trade legs only (3 min)

---

## Decisions (operator sign-off 2026-07-09)

| Topic | Decision |
|-------|----------|
| **VIX/VXN volume** | If Trade channel has no prints, `volume=0` for that bar is acceptable. |
| **Ladder unsubscribe** | **No prune** — keep stale strikes subscribed all day (simple v1). |
| **`IWM` retention** | **Keep IWM** alongside TLT/GLD. |
| **Ladder timing** | Ladder runs only when SPX MQTT is live during the **regular session** (8:30 AM–3:00 PM CT). No pre-market ladder; skip updates until SPX is fresh after open. |

---

*Implemented 2026-07-09. Sidecar kill switch: `MEIC_SIDE_OPTION_COLLECTION=0`.*
