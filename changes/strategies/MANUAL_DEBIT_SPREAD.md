# Manual Tab — Debit Spreads (DB Spread) + Profit Targets

**Status:** DRAFT — not implemented  
**Date:** 2026-08-19  
**Audience:** Operator + implementation  
**Related:** [MANUAL_STRATEGY.md](MANUAL_STRATEGY.md), [MANUAL_BUTTERFLY.md](MANUAL_BUTTERFLY.md), [PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md](PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md) §6, [V2_MODULAR_REWRITE.md](../design/V2_MODULAR_REWRITE.md) §2.4

---

## 1. Purpose

Add **debit vertical spreads** (“DB spread”) to the existing **Manual Spread** dashboard tab so the operator can:

1. Scan OTM strikes around a **target debit** (not credit).
2. Place / modify / cancel working **NET_DEBIT** entry orders (same UX as PCS/CCS today).
3. After fill, run an automated **profit target (PT)** exit — either:
   - **Single PT** — close the full position at one multiple (e.g. 2× entry debit), or
   - **Staged PT** — scale out (e.g. 50% at 2×, remaining 50% at 3×).

Debit spreads are directional long-premium trades. They do **not** reuse the MEIC 2× exchange-stop / Phase 1–3 lifecycle unless explicitly configured. Default exit model is **profit-target-first** (similar to Perma Bull PCS), optionally plus a **debit-side stop** for max loss.

---

## 2. Current state (what exists today)

| Area | Today | Gap for DB spread |
|------|--------|-------------------|
| Manual structures | PCS, CCS, 4× 1-2-1 butterfly | No put/call **debit vertical** |
| Entry scan | `blocks/entry/spread_scan.py` — credit only | No debit scan / ranking |
| Broker open | `place_spread_order()` — STO short + BTO long, **positive** price | Needs NET_DEBIT (negative TT price) with flipped leg actions |
| Trade JSON | `_v2_metadata()` hardcodes `spread_type: 'credit'` | Need `spread_type: 'debit'`, `entry.debit` |
| Stop profile | Credit verticals → `meic_credit_spread`; butterflies → `hold_to_expiry` | Need `manual_debit_spread` (or similar) |
| PT phases | `PhaseProfitTarget` (%), `PhaseSpreadDebitProfitTarget` (resting close debit) — **implemented** | Not wired to Manual; no multi-leg PT |
| Breach / mark math | `blocks/stop/breach.py` — debit branch **done** + tested | Stop monitor close path assumes credit spread close debit formula in places |
| Partial close | Entry partial fills only | **No** scale-out at PT1 then PT2 |
| Dashboard UI | Structure dropdown: credit verticals + butterflies | Add PDS / CDS; PT fields on Place Order |

Explicit prior decision ([MANUAL_STRATEGY.md](MANUAL_STRATEGY.md) §2 Non-Goals v1): *“No debit spreads.”* This document supersedes that for a **v2 Manual** increment.

---

## 3. Structures

Debit verticals use the same strike grid as credit verticals but **flip which leg is bought vs sold**.

| UI label | Side code | Example (width 25, SPX ~7520) | Open action |
|----------|-----------|--------------------------------|-------------|
| Put Debit Spread (PDS) | `PD` | BUY 7525P / SELL 7500P | Pay net debit (bearish / long put side) |
| Call Debit Spread (CDS) | `CD` | BUY 7495C / SELL 7520C | Pay net debit (bullish / long call side) |

**Strike convention (match existing JSON fields):**

| Field | PDS | CDS |
|-------|-----|-----|
| `long_leg` | Higher strike (bought) | Lower strike (bought) |
| `short_leg` | Lower strike (sold hedge) | Higher strike (sold hedge) |
| `side` | `PD` or keep `P` + `spread_type: debit` | `CD` or `C` + `spread_type: debit` |

**Recommendation:** use side codes `PD` / `CD` in filenames and session CSV (mirrors butterfly `SCBF` pattern) while keeping `spread_type: 'debit'` as the authoritative P&L direction flag.

**Mark price (already in codebase):**

```text
debit_mark = long_mid - short_mid     # rises when profitable
entry_debit = fill net paid (positive number)
```

---

## 4. Profit target design

### 4.1 Terminology — “2×” and “3×”

For a debit spread, **N×** means the **spread mark** (position value) reaches **N × entry_debit**:

| Operator says | Entry debit | PT mark | `profit_target_pct` |
|---------------|-------------|---------|---------------------|
| 2× (double) | $1.00 | $2.00 | `1.0` (100% gain) |
| 3× (triple) | $1.00 | $3.00 | `2.0` (200% gain) |
| 50% gain | $1.00 | $1.50 | `0.5` |

Formula (already in `profit_target_hit(..., spread_type='debit')`):

```text
target_mark = entry_debit × (1 + profit_target_pct)
trigger when debit_mark >= target_mark
```

### 4.2 Mode A — Single profit target (v1 minimum)

Close **100%** of filled quantity when the first PT hits.

**Trade JSON (`exit_config`):**

```json
{
  "exit_config": {
    "profit_target_mode": "single",
    "profit_target_pct": 1.0,
    "profit_target_multiple": 2.0
  }
}
```

- Store **`profit_target_multiple`** for dashboard display (2.0 = 2×); derive `profit_target_pct = multiple - 1`.
- Phase: reuse **`PhaseProfitTarget`** with `spread_type='debit'` on the stop profile.
- Execute: `monitor.replace_with_limit_close(reason='profit_target_hit')` or resting spread-close at computed credit — see §6.3.

**Dashboard Place Order (proposed defaults):**

| Field | Default | Notes |
|-------|---------|-------|
| PT mode | `single` | `single` \| `staged` |
| PT multiple | `2.0` | Close when mark ≥ 2× entry debit |
| Stop loss | optional | Off by default for v1; see §4.4 |

### 4.3 Mode B — Staged profit targets (v2)

Scale out across **tiers**. Example: half at 2×, half at 3×.

**Trade JSON:**

```json
{
  "exit_config": {
    "profit_target_mode": "staged",
    "profit_targets": [
      {"multiple": 2.0, "qty_fraction": 0.5, "filled": false},
      {"multiple": 3.0, "qty_fraction": 0.5, "filled": false}
    ],
    "remaining_qty": null
  }
}
```

**Behavior:**

1. On fill, `remaining_qty = filled_quantity`.
2. When mark ≥ 2× entry → close `floor(qty × 0.5)` contracts; mark tier `filled`; decrement `remaining_qty`; **resize or cancel** any exchange stop on the remainder.
3. When mark ≥ 3× entry → close remaining qty; archive trade.

**New code required:**

| Component | Work |
|-----------|------|
| `PhaseStagedProfitTarget` (new) | Walk tiers; call partial close |
| `monitor.replace_with_spread_close(qty=…)` | Today always closes `stop_qty_for_state()` full qty |
| `stop_qty_for_state` / stop resize | After partial PT, stop must cover **remaining** qty only |
| P&L / history | Record partial realized P&L per tier (`close.partial` events) |
| Dashboard | Show active tier, % booked, next PT |

**Qty fraction rules:**

- Fractions must sum to `1.0` (±0.001).
- Last tier closes **all remaining** (handles odd qty: e.g. 3 contracts → 1 + 2 not 1.5 + 1.5).
- Minimum 1 contract per tier when qty ≥ number of tiers.

### 4.4 Optional stop loss (defer or v1.1)

Debit max-loss stop is the mirror of credit breach:

```text
stop when debit_mark <= entry_debit × (1 - stop_loss_pct)
# or absolute: stop_mark <= stop_floor
```

`spread_breach_triggered(..., spread_type='debit')` already supports this direction. If added:

- Register **`Phase1InitialStop`** (or a thin debit variant) **after** PT phase in priority order so PT wins on the same tick when both would fire.
- **Do not** enable MEIC Phase 2 / Phase 3 for debit manual unless product asks for SPX proximity close on long premium.

---

## 5. Architecture — reuse, don’t fork

```text
Manual Spread (today):
  dashboard → session CSV → manual_worker → credit_spread / butterfly → trade JSON → stop_monitor

Manual DB spread (target):
  dashboard → session CSV → manual_worker → debit_spread (new) → trade JSON → stop_monitor
                                                      ↓
                                    stop_profile: manual_debit_spread
                                    exit_config: PT single | staged
```

**Reuse (no rewrite):**

| Module | Reuse |
|--------|-------|
| `common/chain_snapshot.py` | Same Load Chain band |
| `common/strike_guard.py` | Same leg-flip overlap rule on both active dirs |
| `blocks/entry/handshake.py` | Extend or parallel `write_debit_spread_handshake` |
| `blocks/entry/manual_worker.py` | Branch on structure `vertical_debit_p` / `vertical_debit_c` |
| `blocks/stop/phases_profit_target.py` | `PhaseProfitTarget` for single PT |
| `blocks/stop/breach.py` | Debit mark / PT / breach math |
| `blocks/stop/runner.py` | Same dual-dir watch |
| Session CSV + `dispatch_manual_place` | Same queue pattern as credit vertical |

**Do not modify:** MEIC tranche scheduler, `run.py` entry loop, MEIC stop profile defaults.

---

## 6. Implementation checklist

### 6.1 Entry block — `blocks/entry/debit_spread.py` (new)

Mirror `CreditSpreadEntry`:

```python
class DebitSpreadEntry:
    def scan_for_target(side, expiry, lot, *, spread_width, target_debit, max_results, ...)
    def open_candidate(...)  # NET_DEBIT order
    def write_handshake(...)   # spread_type='debit', entry.debit, stop_profile, exit_config
```

**Scan (`blocks/entry/spread_scan.py` or `debit_scan.py`):**

- For each OTM strike pair at fixed width:
  - `market_debit = long_mid - short_mid` (long = bought leg per side)
  - Rank by `abs(market_debit - target_debit)`
- Apply `leg_overlap_conflict` (same as credit manual).
- Enforce `min_market_debit` (config, e.g. $0.05).

**Broker (`brokers/tastytrade_broker.py`):**

Add `place_debit_spread_order(long_symbol, short_symbol, qty, debit)`:

- Legs: **BUY_TO_OPEN** long (expensive), **SELL_TO_OPEN** short (hedge)
- Price: **negative** magnitude per TT SDK (`price=-round(debit)`)
- Journal broker stub for tests

Alternatively extend `place_spread_order(..., spread_kind='debit')` — prefer explicit method to avoid sign mistakes.

### 6.2 Trade JSON / state (`blocks/stop/state.py`)

Extend `_v2_metadata()`:

```python
def _v2_metadata(strategy, stop_profile=None, spread_type='credit'):
    return {
        'spread_type': spread_type,  # 'debit' for DB manual
        ...
    }
```

**Pending / open state fields:**

```json
{
  "spread_type": "debit",
  "stop_profile": "manual_debit_spread",
  "entry": {
    "debit": 1.05,
    "limit_debit": 1.00,
    "net_debit": 1.05
  },
  "exit_config": { "...": "see §4" },
  "long_leg": { "symbol": "...7525P", "strike": 7525, "fill_price": 2.10 },
  "short_leg": { "symbol": "...7500P", "strike": 7500, "fill_price": 1.05 }
}
```

- Stop using `entry.net_credit` for debit trades (keep key for credit only).
- `create_pending_state` / handshake: accept `limit_debit`, `spread_type`, `exit_config`.

### 6.3 Stop profile — `blocks/stop/profiles/manual_debit_spread.py` (new)

```python
MANUAL_DEBIT_SPREAD_PROFILE = 'manual_debit_spread'

def manual_debit_spread_profile() -> StopProfile:
    return StopProfile(
        name=MANUAL_DEBIT_SPREAD_PROFILE,
        spread_type='debit',
        phases=[
            PhaseProfitTarget(),           # single PT (priority 5)
            # PhaseStagedProfitTarget(),   # v2 — staged PT
            # PhaseDebitStopLoss(),        # optional v1.1
        ],
        long_close_delay_sec=0,
    )
```

Register in `blocks/stop/profiles/__init__.py`.

**Close execution audit:** Review `monitor.replace_with_limit_close` and `replace_with_spread_close` for debit positions — opening was NET_DEBIT; closing is BTC/STC reverse combo. Confirm debit limit sign on `place_spread_close_order` when closing a **long** debit spread for a **credit** (profit).

Reference: Perma Bull uses `PhaseSpreadDebitProfitTarget` for **credit** spreads closing at low debit. Manual debit PT likely needs:

- **Mark-based** `PhaseProfitTarget` (primary), or
- Resting close at `target_credit = entry_debit × (multiple - 1)` if we add `PhaseSpreadCreditProfitTarget` for debit exits.

**Action item during implementation:** one integration test with journal broker proving a filled debit spread closes profitably at PT.

### 6.4 Session + worker path

| File | Change |
|------|--------|
| `manual_spread/entry.py` | `scan_spreads` branch for `vertical_debit_p` / `vertical_debit_c`; `place_debit_spread`, `modify_debit_spread` |
| `manual_spread/config.py` | `DEFAULT_TARGET_DEBIT`, `MIN_MARKET_DEBIT`, default PT multiples |
| `blocks/session/manual_helpers.py` | Pass structure + `exit_config` from dashboard |
| `blocks/session/manual_place.py` | `apply_plan_metadata` sets `stop_profile`, `exit_config`; hide `stop_multiplier` for debit (like butterflies) |
| `blocks/entry/manual_worker.py` | Execute debit place + chase (debit chase = **raise** limit toward market, opposite of credit) |

**Chase semantics (debit):** credit chase lowers limit; debit chase **increases** limit debit toward market to improve fill odds. Reuse chase ladder with inverted step sign.

### 6.5 Dashboard (`dashboard/templates/index.html`, `manual_spread_handlers.py`)

**Structure dropdown — add:**

```html
<option value="vertical_debit_p">Put Debit Spread (PDS)</option>
<option value="vertical_debit_c">Call Debit Spread (CDS)</option>
```

**`msOnStructureChange()`:**

- Label: “Target debit $” / “Limit debit”
- Hide MEIC **Stop×** and credit chase defaults (like butterfly)
- Show **Profit target** section:

| Control | Single PT | Staged PT |
|---------|-----------|-----------|
| Mode | radio | radio |
| PT multiple | one input (default 2.0) | tier table: multiple + % qty |
| Preview | “Close @ $X mark (2× $Y entry)” | “50% @ 2×, 50% @ 3×” |

**API `/api/manual_spread/place` payload extension:**

```json
{
  "structure_type": "vertical_debit_p",
  "side": "PD",
  "short_strike": 7500,
  "long_strike": 7525,
  "limit_debit": 1.00,
  "quantity": 2,
  "expiry": "2026-08-19",
  "profit_target_mode": "staged",
  "profit_targets": [
    {"multiple": 2.0, "qty_fraction": 0.5},
    {"multiple": 3.0, "qty_fraction": 0.5}
  ]
}
```

**Active grid columns:** show `PT` (next tier or “2× single”), `Mark`, `Entry debit`, `% to PT`.

### 6.6 P&L, settlement, history

| Area | Work |
|------|------|
| `dashboard/manual_spread_handlers.py` | Mark from `spread_mark_price(..., spread_type='debit')`; entry from `entry.debit` |
| `common/expiry_settlement.py` | Already branches on `spread_type == 'debit'` — verify manual JSON path |
| `dashboard/db.py` | Persist PT tier in close reason / notes for history |
| Discord / alerts | `[MANUAL_SPREAD] PD ms-12 PT1 50% @ 2×` |

---

## 7. Phased delivery

| Phase | Scope | Effort | Delivers |
|-------|--------|--------|----------|
| **P0** | Debit scan + place + modify + cancel; trade JSON `spread_type: debit`; journal tests | Medium | Operator can open DB spread manually |
| **P1** | `manual_debit_spread` profile + **single PT** (`PhaseProfitTarget`); dashboard PT multiple | Medium | Auto exit at 2× (or chosen multiple) |
| **P2** | **Staged PT** — partial close, stop resize, tier state | Large | 50% @ 2×, 50% @ 3× |
| **P3** | Optional debit stop loss; resting limit PT; expiry settlement hardening | Small–Med | Risk cap + polish |

**Suggested MVP:** P0 + P1 (single PT at 2×). Staged multi-PT is the main engineering lift (P2).

---

## 8. Tests

| Test file | Cases |
|-----------|-------|
| `tests/test_debit_spread_scan.py` | Rank by target debit; OTM band; overlap skip |
| `tests/test_debit_spread_place.py` | Handshake JSON fields; negative TT price; side PD/CD |
| `tests/test_manual_debit_profit_target.py` | `PhaseProfitTarget` fires at 2× debit mark; does not fire below |
| `tests/test_staged_profit_target.py` | 2-contract + 2-tier; 3-contract odd qty (P2) |
| `tests/test_breach_math.py` | Already covers debit — extend if stop loss added |
| `tests/test_build_manual_trades.py` | Dashboard row for debit + PT display |

---

## 9. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| Wrong price sign on NET_DEBIT open | Dedicated broker method; assert negative price in tests |
| Credit close math used on debit position | Audit `replace_with_spread_close`; integration test full round trip |
| Partial PT leaves orphan stop qty | Cancel + re-place stop on remainder; block duplicate PT enqueue (V3 exit pool) |
| `short_leg` / `long_leg` confusion vs strike order | Document PD/CD strike mapping; validate in scan builder |
| Staged PT + V3 duplicate close | Reuse flat-account / `close_only_mode` guards from V3 incident fixes |
| Operator expects “2× **profit**” vs “2× **mark**” | Dashboard copy: “Close when spread value reaches 2× entry debit” |

---

## 10. Decisions for operator (before P2)

| # | Question | Options | Recommendation |
|---|----------|---------|----------------|
| D1 | Default PT | Single 2× vs staged 50/50 @ 2×/3× | **Single 2×** for v1 |
| D2 | Stop loss on debit manual? | None / % of debit / absolute mark | **None** for v1 (PT-only exit) |
| D3 | Resting limit at entry vs mark-watched PT | Resting GTC close vs poll MQTT | **Mark-watched** (`PhaseProfitTarget`) — matches existing phase pattern |
| D4 | Side codes | `PD`/`CD` vs `P`/`C` + metadata | **`PD`/`CD`** in lot/filename |
| D5 | Phase 3 SPX proximity on debit | On / off | **Off** — not MEIC |

---

## 11. File touch list (summary)

```
blocks/entry/debit_spread.py          NEW
blocks/entry/debit_scan.py            NEW (or extend spread_scan)
blocks/entry/manual_worker.py         MODIFY — debit branch + chase invert
blocks/entry/handshake.py             MODIFY — debit handshake helper
blocks/stop/state.py                  MODIFY — spread_type param, entry.debit
blocks/stop/profiles/manual_debit_spread.py   NEW
blocks/stop/phases_profit_target.py   MODIFY — PhaseStagedProfitTarget (P2)
blocks/stop/monitor.py                MODIFY — partial spread close qty (P2)
brokers/tastytrade_broker.py          MODIFY — place_debit_spread_order
brokers/journal_broker.py             MODIFY — stub
manual_spread/entry.py                MODIFY — scan/place/modify debit
manual_spread/config.py               MODIFY — defaults
blocks/session/manual_place.py        MODIFY — exit_config on JSON
blocks/session/manual_helpers.py      MODIFY — pass PT fields
dashboard/templates/index.html        MODIFY — structure + PT UI
dashboard/manual_spread_handlers.py   MODIFY — P&L mark, API fields
tests/test_*                          NEW / extend
changes/strategies/MANUAL_DEBIT_SPREAD.md   THIS DOC
```

**Explicitly NOT modified:** `run.py` MEIC scheduler, `strategies/meic/*`, MEIC session CSV schema.

---

## 12. Operator workflow (target)

1. MEIC tab → Pause All (if manual-only day).
2. Manual tab → Load Chain.
3. Structure → **Put Debit Spread**; width 25; target debit $1.00; PT **2× single** (or staged 2×/3×).
4. Search → pick candidate → set limit debit → Place Trade.
5. Fill → stop_monitor watches mark; at **2× entry debit** closes full size (P1) or first tranche (P2).
6. Optional Kill Selected unchanged.

---

## 13. Summary

| Question | Answer |
|----------|--------|
| What is “DB spread” here? | **Debit vertical** (PDS/CDS) on Manual tab |
| Entry | New debit scan + NET_DEBIT order path |
| Single PT | Reuse `PhaseProfitTarget` + `profit_target_pct = multiple - 1` |
| Multi PT (half @ 2×, half @ 3×) | New staged phase + **partial close** (not in codebase today) |
| MEIC impact | None if confined to Manual paths |
| MVP | P0 + P1: open debit + single 2× PT |

**Next step:** Approve D1–D5, then implement **P0 + P1**.
