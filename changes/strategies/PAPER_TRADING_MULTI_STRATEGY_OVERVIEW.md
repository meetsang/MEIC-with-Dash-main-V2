# Paper Trading + New Strategies — Overview

**Status:** IMPLEMENTED — paper-day sign-off 2026-07-31  
**Branch:** `feat/multi-strategy-framework`  
**Date:** 2026-07-28 (spec) · 2026-07-30 (implementation) · 2026-07-31 (paper validation)

For implementation detail, see
[PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md](PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md).

---

## What was built (summary)

The framework is **implemented on branch `feat/multi-strategy-framework`**. MEIC and Manual are unchanged unless you explicitly opt into new features (e.g. portfolio loss cap).

| Capability | Status |
|------------|--------|
| Per-strategy `paper` flag in `config/strategies.yaml` | Done |
| MEIC live + Skyline paper in same launcher | Done |
| Paper safety rails (startup + runtime) | Done |
| Standalone strategy harness (`StandaloneStrategyRunner`) | Done |
| 4-leg combo orders (`place_multi_leg_order`) | Done |
| Skyline strategy (4 butterflies, gap-down, hold-to-expiry) | Done |
| FOMC fix (record prices, skip trading only) | Done |
| Exit Conditions foundation (profit target phase, portfolio guard, manual close) | Done (opt-in; not wired into MEIC profile) |
| Daily overrides API + Skyline Pause Today button | Done |
| Skyline dashboard tab (status, P/L, close buttons, recent history) | Done |
| History tab — all strategies + Paper/Live badge + money filter | Done |
| EOD Skyline settlement → history folder | Done |
| First full paper-day validation | **Done** (2026-07-31 — see `SKYLINE_PAPER_LOG.md`) |
| Spec marked `IMPLEMENTED` | Done |

---

## What problem we solved

Today the bot had **one paper/live switch for everything**. There was no way to keep MEIC live while testing a new strategy on paper. The bot also could not place 4-leg butterfly orders, and the dashboard only knew about MEIC and Manual.

We also fixed: **FOMC days no longer stop price recording** (market is open; only trading is skipped). Real holidays are unchanged — bot does nothing.

---

## Skyline for tomorrow's paper test

### Enable (paper only)

In `config/strategies.yaml`:

```yaml
  - name: SKYLINE_SPX
    enabled: true
    confirmed: true
    paper: true
```

In `.env`:

```
PAPER_MODE=false              # MEIC stays live
TASTYWARE_API_KEY=your_key    # required for paper strategies
```

Restart the launcher before market open.

### What to verify tomorrow

| # | Check | Where |
|---|--------|-------|
| 1 | Launcher starts MEIC live + Skyline paper without broker collision | Logs at startup |
| 2 | Skyline waits until 9:45 AM ET, then attempts entry | Skyline tab |
| 3 | Gap-down skip works if SPX down >25 pts overnight | Skip reason on tab |
| 4 | Four butterflies placed as separate 4-leg orders | Butterflies table |
| 5 | Partial fills visible (not hidden as full success) | Per-butterfly status |
| 6 | Pause Today blocks entry | Skyline → Pause Today |
| 7 | Manual Close / Close All works in paper | Skyline tab buttons |
| 8 | Unrealized P/L updates through the day | Skyline P/L card |
| 9 | EOD settlement moves trade to history (~3:30 PM CT) | `trades/history/SKYLINE_SPX/` |
| 10 | History tab shows Skyline row with **Paper** badge | History → Money: Paper $ |
| 11 | Live MEIC P/L totals exclude Skyline | History → Money: Live $ |
| 12 | FOMC day (if applicable): prices record, no trading | Logs + price CSVs |

### After a successful paper day

1. Log results in `changes/SKYLINE_PAPER_LOG.md` (or a `LIVE_SESSION_...` note).
2. Mark spec status `IMPLEMENTED` in the three framework docs.
3. Decide whether to merge `feat/multi-strategy-framework` → `master`.

---

## Skyline rules (unchanged from spec)

- **Underlying:** SPX 0DTE
- **Entry:** 9:45 AM Eastern
- **Skip if** SPX down >25 pts overnight (no skip for rallies)
- **Four butterflies**, four separate orders (4-leg platform limit)
- **No automatic stop** — defined risk, hold to expiration
- **Manual close** available from dashboard

Strikes are configurable; defaults match the source article (see Technical spec §8.2).

### Live-money path (wired, not for tomorrow)

The code **supports** `paper: false` on Skyline (uses live TastyTrade credentials). Default remains `paper: true`. **Do not enable live Skyline** without a separate operator decision — tomorrow is paper validation only.

---

## Per-strategy paper trading

Each strategy has its own `paper: true/false` in `config/strategies.yaml`.

Paper-only strategies:

- Validate `TASTYWARE_API_KEY` at startup
- Assert broker is paper before every order
- Cannot self-promote to live if misconfigured

Paper trades are labeled **PAPER** in History and excluded from **Live $** totals by default.

---

## History tab (new)

History now supports **all strategies** with a **Paper / Live** column:

- **Live $** (default) — MEIC/Manual real-money trades only
- **Paper $** — Skyline and any future paper strategies
- **All $** — everything combined

Strategy filter includes **Skyline**. Settled Skyline days sync from JSON files into SQLite on refresh.

---

## Exit Conditions (built, opt-in)

Added incrementally — MEIC/Manual unchanged unless configured:

- **Profit target phase** — code exists, not registered in MEIC stop profile
- **Portfolio guard** — per-strategy daily loss cap via `config_overrides.max_daily_loss`
- **Manual close** — generic N-leg close for standalone strategies (Skyline uses this)

---

## Dashboard overrides

- **Permanent defaults** — `config/strategies.yaml`
- **Today only** — dashboard overrides in `session_<date>.json`
- **Skyline Pause Today** — button on Skyline tab (calls override API)
- Full overrides UI (edit arbitrary keys from dashboard) — **not built yet**

---

## Known limitations (still true in v1)

| Issue | Status |
|-------|--------|
| Backtest unverified | Paper-first by design |
| Partial butterfly fills | Shown per-structure; no chase |
| 4 separate orders (not netted) | Platform 4-leg limit |
| Dashboard still MEIC/Manual centric on Today tab | Skyline has own tab; History generalized |
| Skyline not on `stop_monitor` | Uses own JSON + dashboard MTM |
| Settlement P/L math may need tuning | Observe on paper day |
| `PhaseProfitTarget` not in any live profile | Opt-in when needed |
| Spec docs said no live Skyline path | **Updated:** live path wired but default off |

---

## Build phases (updated)

| Phase | Description | Status |
|-------|-------------|--------|
| 1 | Per-strategy paper + FOMC fix | Done |
| 2 | Standalone + N-leg + Exit Conditions foundation | Done |
| 2.5 | Daily overrides API | Done |
| 3 | Skyline + dashboard + EOD settlement | Done |
| 3b | History tab paper/live + Skyline sync | Done |
| 4 | Paper-day sign-off + docs `IMPLEMENTED` | **Done** (2026-07-31) |

---

## Pending after paper test

Pick these up after tomorrow validates the happy path:

1. **Paper-day log** — `changes/SKYLINE_PAPER_LOG.md` with fills, skips, settlement, manual close notes
2. **Mark spec IMPLEMENTED** — update status in all three framework docs
3. **Merge branch** — `feat/multi-strategy-framework` → `master` if satisfied
4. **Overrides UI** — dashboard controls beyond Pause Today (POST arbitrary override keys)
5. **`PhaseProfitTarget`** — register in a stop profile if MEIC profit-target is wanted
6. **`stop_monitor` monitor-only** — optional: attach Skyline to `hold_to_expiry` profile for unified MTM
7. **Loader/validation tests** — extend `test_strategy_loader.py` for Skyline `confirmed`/`paper`
8. **Settlement P/L math** — verify premium-point × 100 multiplier matches broker reality
9. **Full test suite** — run in venv with `paho-mqtt`, `pandas_market_calendars`
10. **Portfolio guard on MEIC** — opt-in via yaml if desired after paper validation

---

## Success criteria

| Criterion | Status |
|-----------|--------|
| MEIC live + Skyline paper concurrently | Built — verify tomorrow |
| Skyline cannot place live orders when `paper: true` | Built — verify tomorrow |
| Skyline off until `enabled` + `confirmed` | Built |
| Four-butterfly layout from article | Built |
| No automatic stop; manual close available | Built |
| FOMC days record prices, skip trading | Built |
| Paper excluded from live P/L by default | Built |
| MEIC/Manual unchanged | Built — verify tomorrow |
| Full paper day end-to-end | **Pending tomorrow** |

---

## Decision log

| Date | Decision |
|------|----------|
| 2026-07-28 | Initial plan: incremental, not V4 rewrite. Skyline paper-first. |
| 2026-07-30 | Split Overview + Technical. Exit Conditions, FOMC fix, `enabled`+`confirmed` (no `.env`). |
| 2026-07-30 | FOMC fix scoped to open-market days only; holidays unchanged. |
| 2026-07-30 | **Implementation complete on `feat/multi-strategy-framework`.** History tab generalized with paper/live flag. Skyline live-money path wired (`paper: false` supported) but default remains paper — operator will not enable live for first test. Phase 4 (paper-day sign-off) deferred to next session. |
