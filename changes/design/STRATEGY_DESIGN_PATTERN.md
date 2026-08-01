# Strategy Design Pattern — Reference for Every New Strategy

**Status:** LIVING DOCUMENT — read before adding or changing any strategy
**Date:** 2026-07-31
**Related:** [MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md](MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md) (long-term V4 architecture lab — separate-repo, not live), [../strategies/PAPER_TRADING_MULTI_STRATEGY_FRAMEWORK.md](../strategies/PAPER_TRADING_MULTI_STRATEGY_FRAMEWORK.md) / [_OVERVIEW.md](../strategies/PAPER_TRADING_MULTI_STRATEGY_OVERVIEW.md) / [_TECHNICAL.md](../strategies/PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md) (implemented framework), [../strategies/SKYLINE_PAPER_LOG.md](../strategies/SKYLINE_PAPER_LOG.md) (first strategy shipped on this pattern). Index: [../README.md](../README.md).

## 1. Purpose

Every time we build or materially change a strategy, we should be moving **one small, additive step** toward a consistent shape — not reinventing plumbing per-strategy, and not touching the MEIC/Manual live path to do it. This doc is the thing to open first. It answers:

1. What shape does my new strategy need? (§4)
2. What already exists that I should reuse, not rebuild? (§3)
3. Where are we trying to end up, long-term, and how does this change move us there? (§5)
4. What's the standing backlog of "next small step" items, so individual strategy work keeps chipping away at it? (§6)
5. What's the checklist / definition-of-done before calling a new strategy shipped? (§7)

This is a living document — update it whenever a strategy addition teaches us something new about the pattern (a new leg shape, a new exit style, a new config need), rather than letting that knowledge live only in one strategy's code.

## 2. The two rules that never change

1. **MEIC_IC and MANUAL_SPREAD's live code paths are never modified to accommodate a new strategy.** Session-CSV, `EntryMonitorRunner`, and the vertical-spread `stop_monitor` phases keep working exactly as they do today, unconditionally. Every change described in this doc is **additive** — new modules, new optional branches gated on config, never a rewrite of the live path. If a change can't be made additively, it doesn't belong in this framework; it belongs in a V4-style separate system (§5), proposed and reviewed on its own.
2. **A new, unproven strategy ships paper-only by default**, with an explicit two-key opt-in (yaml `enabled: true` **and** a config-level `confirmed: true` / env acknowledgment — see `strategies/validate.py`). Promotion to live is a distinct, deliberate decision with its own sign-off, never a side effect of a code change.

Everything else in this doc is guidance, not law — but violating §2 requires an explicit, written exception, not a quiet workaround.

## 3. Current architecture (as-built) — what to reuse

| Concern | Existing building block | Where |
|---|---|---|
| Strategy identity/config | `StrategyConfig` dataclass + `BaseStrategy` ABC (`run`/`stop`/`get_status`) | `strategies/base.py` |
| Loading + per-strategy paper flag + config overrides | `load_strategies()`, `apply_config_overrides()` | `strategies/loader.py` |
| Startup validation / two-key opt-in | `validate_startup_config()` | `strategies/validate.py` |
| Per-strategy broker, live/paper coexist | `get_shared_broker_for_strategy(name, paper=...)` | `common/broker_factory.py` |
| Journal-paper (no Tastyware `PaperSession`, live data + local fills) | `JournalBroker` | `brokers/journal_broker.py` |
| N-leg combo orders (butterflies, condors, etc.) | `ComboLeg` + `place_multi_leg_order()` | `brokers/base.py`, `brokers/tastytrade_broker.py` |
| Non-tranche strategy lifecycle: broker resolution, daily idempotency, crash-safe resume, status | `StandaloneStrategyRunner` | `common/standalone_runner.py` |
| Per-strategy trade storage | `active_dir_for_strategy()` / `history_dir_for_strategy()` | `common/trades_layout.py` |
| Exit management — phase-based, vertical-spread shaped | `StopProfile` + `PhaseBase` subclasses | `blocks/stop/stop_profile.py`, `blocks/stop/phases.py` |
| Exit management — monitor-only / no active stop (defined-risk) | `hold_to_expiry_profile()` | `blocks/stop/profiles/hold_to_expiry.py` |
| Per-strategy daily loss halt | `PortfolioGuard` | `common/portfolio_guard.py` |
| Temporary same-day config tweaks from the dashboard | `daily_overrides.effective_config()` | `common/daily_overrides.py` |
| Dollar PnL from premium-point PnL (non-vertical strategies) | `pnl_points_to_dollars()`, `attach_dollar_pnl_display()` | `strategies/skyline/settlement.py` (pattern — not skyline-specific, copy the shape) |
| Streamer subscription for a strategy's option legs | `register_option_symbols()` | `common/streamer_symbols.py` |

**Two supported strategy shapes today:**

| Shape | Examples | Entry | Exit | Storage |
|---|---|---|---|---|
| **Tranche / session-CSV** | MEIC_IC, MANUAL_SPREAD | `EntryMonitorRunner` reads session CSV row | `stop_monitor` phases (2-leg vertical only) | `trades/{active,history}/<NAME>/*.json`, one file per slot |
| **Standalone** | SKYLINE_SPX | Strategy's own `run()`, wrapped by `StandaloneStrategyRunner` | Strategy-owned; `hold_to_expiry` for defined-risk, or none | `trades/{active,history}/<NAME>/*.json`, one file per structure/day |

Don't invent a third storage/lifecycle pattern unless a strategy genuinely can't fit either shape — extend `StandaloneStrategyRunner` first.

## 4. Decision tree for a new strategy

1. **Is it a single short/long credit vertical, entered on a fixed intraday schedule of tranches?** → Tranche shape. Reuse `EntryMonitorRunner` + a `StopProfile` with real phases. (This should be rare going forward — MEIC/Manual already own this shape.)
2. **Is it anything else** (multi-leg combo, single daily entry, custom cadence, custom exit logic)? → **Standalone shape.** Wrap it in `StandaloneStrategyRunner`, give it its own `trades/{active,history}/<NAME>/` files, and pick an exit style:
   - **Defined-risk, no active management wanted** (e.g. Skyline) → `hold_to_expiry` profile, or attach no stop profile at all.
   - **Defined-risk but wants an active time-boxed or price-boxed exit** (e.g. a future event-day strategy, §6 of the reverse-MEIC research doc) → the strategy's own `run()` loop owns the exit condition; it does **not** need `stop_monitor` at all if the exit rule is simple (time-based, or a single mechanical check). Only reach for phase-based `stop_monitor` machinery if the exit genuinely needs multi-condition, restart-safe, per-leg order management — that machinery is heavy and vertical-shaped today (§5).
3. **New order shape needed (3+ legs, ratios, etc.)?** Extend `ComboLeg`/`place_multi_leg_order`, don't hand-roll SDK calls in strategy code.
4. **New paper-fidelity need** (want live-data fills without a broker paper account)? Reuse `JournalBroker`, don't build a second one.

## 5. North star — where the incremental steps are headed

The [V4 doc](MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md) already describes the fully-generalized target shape in a separate-repo lab (`InstrumentSpec`, `OrderIntent`, `PositionState`, `ExitPolicy`). We are **not** doing that rewrite in the live V3 codebase. Instead, every incremental addition here should, where practical, be a small step in that direction rather than a step away from it:

| V3-pragmatic construct (today) | V4 concept it's a special case of | Direction to lean, incrementally |
|---|---|---|
| `short_leg`/`long_leg` fields on a vertical trade JSON | `PositionState.legs: List[Leg]` | New strategies should store legs as a **list**, not fixed named fields (Skyline's `structures[].legs[]` already does this) |
| `StopProfile.phases` (vertical-only breach math) | `ExitPolicy` (leg-count-agnostic) | Next generalization: make phase breach math read from a leg list + a `mark()`/`entry_price()` interface instead of `short_leg`/`long_leg` directly, so a 4-leg structure could eventually opt into phase-based exits without new phase classes per shape |
| `place_spread_order` / `place_multi_leg_order` (two separate primitives) | one `OrderIntent` → `BrokerAdapter` | Fine as-is; don't add a third order primitive — extend `place_multi_leg_order` if a gap appears |
| Per-strategy hardcoded trade JSON schema (`structures[]` for Skyline, flat fields for MEIC) | one `PositionState` schema | Don't force old strategies to migrate; **do** default new non-vertical strategies to a `legs: List[{symbol, action, qty, fill_price}]` shape so a future schema unifier has less to reconcile |
| Config-driven strike offsets (`config_overrides`) | `EntryPolicy`/`SignalGate` | Next generalization: a small, strategy-agnostic "rule table" for entry/exit *conditions* (not just static params) — see §6.2 |

## 6. Standing backlog — next small steps (pick these up opportunistically, not all at once)

Each item below is scoped to be additive and independently shippable. Do them when a concrete strategy needs them, not speculatively.

### 6.1 Leg-agnostic exit phase interface

Today `blocks/stop/breach.py`/`phases.py` assume `state['short_leg']`/`state['long_leg']`. Before a second standalone strategy needs *active* (non-hold-to-expiry) exit management, generalize the phase interface to operate on `state['legs']: List[dict]` with a `mark(legs) -> float` / `entry_price(legs) -> float` pair, and make the vertical case (`short_leg`/`long_leg`) a thin adapter that builds a 2-item `legs` list. MEIC/Manual's trade JSON and behavior do not change — only the internal phase math is regeneralized, verified against the existing MEIC unit tests before/after.

### 6.2 Config-driven condition rules (the "Excel grid" pattern)

From the Excel-trader review: strategies like MEIC conceptually have several named row-actions (base entry, roll, avg-up/down, close-remainder) each gated by boolean conditions (time, price, state). Rather than a big rule-engine rewrite, extend `config_overrides` with an optional, strategy-scoped **rule table** — a list of `{name, when: {time_after, time_before, spx_move_gt, ...}, action}` entries evaluated by one small shared evaluator — for the parts of a strategy that are genuinely "tune a threshold" style (e.g. Skyline's gap-down skip, a future event-day time-boxed exit). Keep strategy `run()` methods as real Python; this is for the repeatable numeric/time conditions only, not a full visual-grid rewrite.

### 6.3 Trade-schema convergence (long-term, no forced migration)

Don't touch MEIC/Manual's flat schema. Do make every new standalone strategy's trade JSON use a `legs[]` list per structure (already true for Skyline) so that if/when a schema unifier is built, only the vertical strategies need an adapter, not every future one too.

### 6.4 Dashboard generalization

`dashboard/server.py`'s `STRATEGY_MEIC`/`STRATEGY_MANUAL` branches are not rewritten wholesale (tracked as non-blocking cleanup in the framework spec). Each new standalone strategy gets its own read-only card (as Skyline did) rather than waiting for a generic loop. Build the generic loop only once there are 3+ standalone strategies and the duplication is clearly a maintenance cost, not before.

## 7. Checklist for every new strategy

1. Pick a shape (§4). Default to standalone unless it's genuinely a session-CSV vertical.
2. `strategies/<name>/strategy.py`: `<Name>Config(StrategyConfig)` + `<Name>Strategy(BaseStrategy)`.
3. Register in `config/strategies.yaml`: `enabled: false` by default, `paper: true` by default, `scheduled: false` if standalone.
4. If standalone: wire through `StandaloneStrategyRunner` in `run.py` (additive line, not a branch inside MEIC's loop).
5. If new leg shape: extend `ComboLeg`/`place_multi_leg_order`, not ad-hoc broker calls.
6. Pick an exit style: `hold_to_expiry` (defined-risk, no touches), a simple time-boxed exit owned by the strategy's own loop, or (only if truly needed) new phase classes.
7. Trade JSON: use a `legs[]`/`structures[]` shape (§6.3), include an explicit `"paper": true|false` field.
8. Register option legs with the streamer (`common/streamer_symbols.register_option_symbols`) so `options_quotes.csv` captures fills/marks for later PnL reconstruction.
9. Dollar PnL: reuse the `pnl_points_to_dollars`/`attach_dollar_pnl_display` pattern rather than displaying premium points on the dashboard.
10. Add a dashboard card (read-only first) + a `PAPER` badge; never blend paper PnL into live totals.
11. Tests: loader picks it up (`test_strategy_loader.py`), any pure leg-building functions are unit-testable without a broker (mirrors `strategies/skyline/legs.py`), and `StandaloneStrategyRunner` resume/idempotency behavior is covered (mirrors `test_standalone_runner.py`).
12. Write (or extend) a short paper-day log once it runs live-data paper for real (mirrors `SKYLINE_PAPER_LOG.md`) before ever discussing a live path.
13. Update §6 above if this strategy exposed a new generalization opportunity.

## 8. Definition of done for "shipped" (paper)

- Loads via `config/strategies.yaml` with `enabled: false` default, runs cleanly end-to-end for at least one full session with `enabled: true` set deliberately for a test day.
- Trade JSON + dashboard card show entries/fills/PnL correctly, in dollars, with a visible PAPER badge.
- No modification to MEIC/Manual's live-path files was required to ship it (grep the diff against `blocks/entry/`, `blocks/stop/monitor.py` non-additively — any touch there needs a specific justification, not just "it was convenient").
- A short paper-day log exists documenting the first real run, incidents, and fixes.
- Promotion to live money is explicitly out of scope until a separate decision is made — this doc does not grant that permission.
