# Paper Trading + Multi-Strategy Framework — Technical Spec

**Status:** IMPLEMENTED — paper-day sign-off 2026-07-31  
**Branch:** `feat/multi-strategy-framework`  
**Date:** 2026-07-28 (spec) · 2026-07-30 (implementation) · 2026-07-31 (paper validation)

**Plain-English summary:**
[PAPER_TRADING_MULTI_STRATEGY_OVERVIEW.md](PAPER_TRADING_MULTI_STRATEGY_OVERVIEW.md)

**Related:**
[MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md](MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md)
(prior full-rewrite attempt — see §1.7, why this doc explicitly does not repeat it),
[MANUAL_STRATEGY.md](MANUAL_STRATEGY.md),
`strategies/`, `common/broker_factory.py`, `brokers/tastytrade_broker.py`,
`blocks/stop/*`, `common/expiry_settlement.py`, `run.py`

**Priority:** P1 — Skyline (§8) without touching MEIC live safety code

---

## 0. Reading guide

This is a big document because the operator asked for maximum
implementation detail so this succeeds where the V4 rewrite did
not (§1.7). It is organized as five additive parts, each of
which can be built, tested, merged, and rolled back **independently**:

| Part | What | Depends on |
|---|---|---|
| A (§3) | Per-strategy paper trading | Nothing new |
| B (§4) | Standalone strategy contract + N-leg orders | Part A (broker resolution) |
| C (§5) | FOMC-day fix — keep recording prices on FOMC/day-after-FOMC days; real holidays are unaffected | Nothing — standalone bugfix |
| D (§6) | Exit Conditions framework (profit target, portfolio loss cap, manual close) | Part B (for Skyline's manual close) but otherwise independent |
| E (§7) | Daily dashboard overrides (config precedence) | Nothing new, generalizes an existing pattern |
| F (§8) | Skyline strategy itself | Parts A, B, C, D (partially), E |

No part requires renaming or restructuring existing modules.
Every new capability is **opt-in per strategy** via config.
MEIC and Manual Spread's code paths are not modified by any part
of this document unless explicitly noted.

### Implementation status (2026-07-30)

Phases 1–3 and History-tab work are **implemented** on branch
`feat/multi-strategy-framework`. Phase 4 (operator paper-day
sign-off) is the only remaining gate before marking this spec
`IMPLEMENTED`.

See **§16** (file-by-file status) and **§17** (tomorrow's paper
test checklist + post-test backlog).

**Deviations from original draft:**

| Original spec | As built |
|---|---|
| Skyline has no live path (§2 non-goals) | Live path **wired** (`paper: false` + live TT creds); default remains `paper: true`; not enabled for first test |
| `.env SKYLINE_ENABLED` (§5, §8.4) | **Rejected** — `enabled` + `confirmed` in yaml only |
| `blocks/stop/profiles/skyline.py` separate file | Registered in `hold_to_expiry.py` (same factory) |
| Dashboard Skyline read-only (§8.8) | Close buttons + Pause Today added |
| History only via Skyline card | **History tab** syncs all strategies; `paper` column + `?money=live\|paper\|all` |
| `confirmed: true` default in yaml | `confirmed: false` until operator opts in |

---

## 1. Problem statement

### 1.1 Paper trading today is a single global switch

`PAPER_MODE` (`.env`) is one process-wide boolean (`common/tt_config.py:10`). `common/broker_factory.get_broker()` reads it once per broker creation. There is no way to run **MEIC_IC live** and a **new strategy on paper** in the same launcher process — turning `PAPER_MODE=true` on paper-trades everything (including MEIC/Manual), and `false` live-trades everything. `StrategyConfig.paper` (`strategies/base.py:17`) exists as a per-strategy field but:

- `strategies/loader.py:load_strategies()` never reads a `paper:` key from `config/strategies.yaml` and never sets `cfg.paper` — the field is always its dataclass default (`False`).
- Nothing downstream (`common/broker_factory.get_broker`, `get_shared_broker`) is ever called with a strategy-scoped `paper` argument except `IronFlyStrategy.run()` (`strategies/iron_fly/strategy.py:58`), which is dormant (not registered in `strategies.yaml`).
- `get_shared_broker()` keys its single cached instance on `(BROKER, paper, pid)` — one shared broker per process, so even if a strategy passed `paper=True` explicitly it would collide with (or evict) the shared live broker used by MEIC/stop_monitor.

### 1.2 The live path is hard-coded to one strategy shape

`run.py`, `blocks/entry/runner.py`, `blocks/session/plan.py`, `blocks/stop/*`, and `dashboard/server.py` are all built around **exactly two strategies**, both 2-leg credit verticals sharing one mechanism:

- Entry: session CSV row → `EntryMonitorRunner` → `CreditSpreadEntry` (short+long, single credit, single stop-multiplier).
- Exit: `stop_monitor` phases (`Phase1InitialStop`, `Phase2NetCreditUpgrade`, `Phase3SpxProximityClose`) — all assume a short/long vertical with one active exit order.
- Storage: `common/trades_layout.py` hard-codes `STRATEGY_MEIC` / `STRATEGY_MANUAL` and `active_dir_for_strategy()` / `history_dir_for_strategy()` branch on exactly those two names.
- Dashboard: `dashboard/server.py` branches on `STRATEGY_MEIC` / `STRATEGY_MANUAL` literally (PnL keys `meic_pnl`/`manual_pnl`, `meic_trades`/`manual_trades`, etc.) — there is no strategy-agnostic loop.

`strategies/base.py`, `strategies/loader.py`, `strategies/validate.py`, `config/strategies.yaml`, and the `strategies/iron_fly/` scaffold already provide a **registry** for pluggable strategies, but only the 2-leg credit-vertical shape is actually wired into the live launcher. Any new strategy whose structure isn't a single short/long vertical (e.g. a 4-leg butterfly) has nowhere to plug in today.

### 1.3 No N-leg order support (and a real platform ceiling)

`brokers/base.py` / `brokers/tastytrade_broker.py` expose `place_limit_order` (1 leg), `place_spread_order` / `place_spread_close_order` (2 legs, short+long). There is no combo-order primitive for 3+ legs (e.g. a butterfly: long 1 / short 2 / long 1 on one ticket). This blocks any strategy built from butterflies, condors-of-more-than-2-legs, etc.

**Operator-confirmed constraint:** TastyTrade (like most retail options platforms) caps a single combo order ticket at **4 legs**. This means "netting" Skyline's four butterflies into fewer tickets is not simply a v1-simplicity trade-off we could revisit later with more engineering effort — it is a **hard platform ceiling**. Even a future netting optimization could only ever combine legs that happen to coincide across butterflies *up to 4 legs per resulting ticket*, never a single 16-leg "super ticket." This is reflected in the non-goals (§2) and the follow-ups list (§10).

### 1.4 Exit management is one-shape-only: fixed stop-multiplier on a 2-leg vertical

`blocks/stop/stop_profile.py` (`StopProfile` dataclass) and `blocks/stop/phases.py` (`PhaseBase` plugin system) already support **pluggable phases per strategy** in principle — `MEICStrategy.stop_profile()` and `ManualSpreadStrategy.stop_profile()` both call the same `meic_stop_profile()` factory today (`blocks/stop/profiles/meic.py`), but nothing stops a different strategy from registering a different phase list. However, in practice:

- Only stop-loss phases exist (`Phase1InitialStop`, `Phase2NetCreditUpgrade`, `Phase3SpxProximityClose`) — there is no profit-target phase.
- Everything assumes `spread_type='credit'`; `spread_type='debit'` is explicitly noted as "deferred" in the dataclass (`blocks/stop/stop_profile.py:16`) and never implemented.
- There is no concept of a **strategy-wide** exit condition (e.g. "close everything and stop opening new trades if this strategy is down $2,000 today") — every phase operates on one open position (`monitor.state`) at a time, with no aggregate view across a strategy's positions.
- There is no **manual** close action independent of the phase system — an operator cannot say "close this one position/structure right now" outside of the automated phase evaluation loop (dashboard-triggered emergency closes exist for MEIC verticals via `place_spread_close_order(..., allow_unverified_emergency_close=...)`, but there's no equivalent for a multi-leg combo).

This matters for Skyline specifically: it needs (a) no automatic stop-loss at all (defined-risk by construction) and (b) a manual "close all 4 butterflies" / "close butterfly N" action for operator use while monitoring — neither of which the current phase system provides, and both of which should be built as **generic, reusable primitives**, not Skyline-specific code.

### 1.5 `check_trading_day()` conflates "market is closed" with "market is open but we choose not to trade" — the fix must not treat them the same

`run.py:main()` (lines ~390–397) runs `check_trading_day()` before starting anything else, and treats every `False` result identically:

```python
def check_trading_day():
    ...
    if today_str in market_closed:
        return False, f"NYSE is CLOSED today ({today_fmt}). No trading."
    if today_str in fomc_days:
        return False, f"Today ({today_fmt}) is a FOMC day. Skipping trading."
    if yesterday_str in fomc_days:
        return False, f"Yesterday ({...}) was a FOMC day — skipping the day after. No trading."
    return True, f"{today_fmt} is a normal trading day. Proceeding."
```

```python
if not force:
    should_trade, reason = check_trading_day()
    if not should_trade:
        terminal_info(log, reason)
        _write_status('skipped', reason)
        return          # <-- exits main() entirely, for all three False cases alike
```

These three `False` branches are **not equivalent**:

- **Real NYSE holiday (`market_closed`):** the exchange itself is shut. SPX does not trade, there are no quotes to record, and correctly *nothing* should run — `run.py` exiting immediately, exactly as it does today, is the right behavior. Confirmed directly with the operator: *"on holidays, there is no point of running anything though, SPX prices won't be changing."* This is **not a bug** and must not change.
- **FOMC day / day-after-FOMC (`fomc_days`):** the exchange is **open**. SPX trades normally all day and prices genuinely move tick to tick — `check_trading_day()` returning `False` here means only "our strategies choose to sit this day out," not "the market is closed." Exiting `main()` entirely on these days *is* a bug: it silently stops recording real, moving SPX prices on exactly the days (FOMC-adjacent) most likely to be worth reviewing later, and it starves Skyline's own gap-down check (§8.3) of a prior close if the FOMC day happens to fall right before a trading day.

The fix (§5) touches **only** the second bucket. On a genuine holiday, `run.py` continues to exit immediately with zero change from today's behavior — this is the key correction the operator's feedback drove: the earlier draft of this fix would have kept `run.py` running (streamer, market_data, stop_monitor) on real holidays too, which is pointless and was wrong.

### 1.6 Config precedence has no "today only" override concept outside MEIC's session CSV

MEIC already solves "operator wants to change behavior for today without editing code/config" via the session CSV's `paused`/`skip` columns (`blocks/session/plan.py`, `SessionRow.paused`/`skip`), which are:

- Seeded from code defaults each morning (`blocks/session/bootstrap.py:_default_rows()`, e.g. `MEIC_DEFAULT_PAUSED_LOTS`).
- Editable by the dashboard for the current session only.
- Rebuilt fresh from defaults the next morning (`bootstrap_meic_session_if_missing` only creates the file if missing — a new day means a new file).

This pattern is **not generalized** — Manual Spread, the standalone-strategy contract (§4), and Skyline all need an equivalent "dashboard can change X for today, permanent config is untouched" mechanism, but today it only exists inside the MEIC session-CSV code path. §7 generalizes it.

### 1.7 Relationship to the V4 document — and why this document is deliberately different

[MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md](MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md) already identifies gaps overlapping §1.2–1.4 and proposes a full separate-repo rewrite (`InstrumentSpec`, `OrderIntent`, `PositionState`, `ExitPolicy`, etc.).

**Operator-confirmed outcome:** the V4 rewrite attempt "went sideways" — attempting to rewrite the trading/exit engine from scratch in one large effort ended up destabilizing functionality that was already working in the live V3 system. That outcome directly shapes every decision in this document. Concrete guardrails applied throughout, specifically because of that experience:

1. **No renames or restructuring of working modules.** `blocks/stop/stop_profile.py`, `blocks/stop/phases.py`, `blocks/stop/profiles/meic.py`, `brokers/tastytrade_broker.py`, etc. keep their existing names, classes, and function signatures. "Exit Conditions" (§6) is a **config/documentation-facing concept**, layered on top of the existing `StopProfile`/`PhaseBase` classes via a new *alias* config key and new *additional* phase classes — it is not a rewrite of those classes.
2. **Every new capability ships and is testable independently.** Parts A–F (see §0 table) do not require each other to be merged in a single PR; each has its own test file and can be reverted independently without breaking the others.
3. **Every new capability is opt-in per strategy via config**, defaulting to today's exact behavior. MEIC_IC and MANUAL_SPREAD are never migrated onto new Exit Condition phases, the standalone runner, or anything else in this document unless a human explicitly edits their yaml entry to do so — and that is out of scope for this document (§2 non-goals).
4. **No "big bang" cutover.** There is no phase in §9 where existing functionality is temporarily broken while a new system is built underneath it; each phase adds a new, initially-unused code path and wires it in last (feature-flagged / disabled-by-default) rather than swapping an existing path out from under running strategies.
5. **Scope is capped to what Skyline actually needs**, not "the ideal general architecture." Where V4 tried to design `PositionState`/`ExitPolicy`/`SignalGate` abstractions for hypothetical future strategies, this document builds only: N-leg orders (needed now), a profit-target phase + portfolio loss cap + manual close (needed now, described concretely in §6, not left as an abstract interface), and a standalone-strategy runner (needed now). Anything not needed for Skyline is explicitly deferred (§10) rather than speculatively built.

---

## 2. Goals / non-goals

### Goals

1. A strategy's `paper` flag is authoritative and **per-strategy**, set in `config/strategies.yaml`, independent of the global `PAPER_MODE` toggle.
2. A strategy flagged `paper: true` can **never** place a live-account order — enforced defense-in-depth at the broker layer, not just by config convention.
3. MEIC_IC (live) and a new paper strategy can run **concurrently in the same launcher process** without broker/session collisions.
4. A documented, minimal contract for adding a **standalone strategy** (one that is not a 2-leg credit vertical and does not use the session-CSV/stop_monitor mechanism) — generalizing the currently-dormant `IronFlyStrategy` pattern.
5. A generic N-leg combo order primitive on the broker abstraction (`place_multi_leg_order`), so butterflies/condors/etc. can be built without new per-strategy broker hacks, and can also be used to **close** a combo (reverse actions), not just open one.
6. Per-strategy trade storage (`trades/active/<NAME>/`, `trades/history/<NAME>/`) and dashboard visibility that scale beyond the current two hard-coded strategies, with a clear "PAPER" badge.
7. Fix the FOMC-day bug so SPX price recording continues on FOMC/day-after-FOMC days (market open, strategies choosing to sit out); real NYSE holidays keep today's fully-off behavior, unchanged (§5).
8. Extend the exit system with **profit targets**, a **strategy-wide loss cap**, and a **manual close action** — as additive phases/primitives on top of the existing `StopProfile`/`PhaseBase` system, not a replacement of it (§6).
9. Generalize the "dashboard can override for today, config holds the permanent default" pattern already used by MEIC's session CSV, so it applies to any strategy's config values (§7).
10. Ship the **Skyline** multi-butterfly 0DTE SPX strategy (§8) as the first strategy built on this framework: paper-only, no automatic stop/exit management (defined-risk, hold-to-expiration) but with manual close controls, disabled by default.

### Non-goals (v1)

- Rewriting MEIC_IC or Manual Spread onto the new standalone pattern or onto new Exit Condition phases — they keep using session CSV + `EntryMonitorRunner` + `stop_monitor` + `meic_stop_profile()` exactly as today, unless an operator explicitly opts a strategy's yaml entry into something new later (separate future decision).
- The full V4 `InstrumentSpec`/`PositionState`/`ExitPolicy` abstraction layer (§1.7).
- Combining Skyline's legs into fewer than 4 broker tickets (v1 submits 4 independent 4-leg orders — see §8.5). Even future netting work is capped at 4 legs/ticket by the broker platform itself (§1.3), not by this document's ambition.
- Any live-money path for Skyline. There is no flag, override, or promotion path in this spec that lets Skyline place a non-paper order. If Skyline is ever proposed for live trading, that requires a **new** design doc and explicit operator sign-off — not a follow-on to this one.
- Discretionary position adjustments (adding condors to lift the "valley", adding a 5th butterfly on a breakout side) described in the source article — v1 is strictly the static 4-butterfly entry, held to expiration (or manually closed), no automated touches.
- **Automatic** stop-loss for Skyline. Manual close (all or one butterfly) is in scope; an automated trigger that decides to close Skyline on its own is not.
- A generic, strategy-agnostic portfolio-risk engine across *all* strategies combined (e.g. "total account loss across MEIC + Manual + Skyline"). §6.4's loss cap is **per strategy**, not cross-strategy, in v1.
- Real-time P/L-based alerting/greeks dashboards for Skyline beyond what's needed to see it filled and see it settle.
- Multi-account brokers other than TastyTrade (Schwab stays out of the new-strategy path, consistent with `broker_factory.get_broker`).
- Any external market-data vendor (e.g. Yahoo Finance) for Skyline's gap-down check — it must use data the bot already records (§8.3).

---

## 3. Part A — Per-strategy paper trading

### 3.1 Config: `paper` becomes a first-class per-strategy field

`config/strategies.yaml` entries gain an explicit `paper` key (defaults to current global `PAPER_MODE` behavior when omitted, so MEIC_IC/MANUAL_SPREAD are unaffected):

```yaml
strategies:
  - name: MEIC_IC
    enabled: true
    module: strategies.meic.strategy
    instrument: SPX
    broker: tastytrade
    stop_profile: meic_credit_spread
    # paper: omitted -> falls back to global PAPER_MODE (unchanged behavior)

  - name: SKYLINE_SPX
    enabled: false          # must be true to load at all — see §8.4
    confirmed: true         # explicit operator acknowledgment — see §8.4
    module: strategies.skyline.strategy
    instrument: SPX
    broker: tastytrade
    stop_profile: skyline_hold_to_expiry   # aliasable as exit_profile — see §6.1
    scheduled: false        # not a session-CSV tranche strategy (§4)
    paper: true             # hard-pinned — see §3.3
```

`strategies/loader.py:load_strategies()` must be extended to:

1. Read `entry.get('paper')`; if present, pass it to `cfg_cls(..., paper=bool(entry['paper']))`; if absent, leave the dataclass default so callers can fall back to `tt_config.PAPER_MODE` (unchanged today).
2. Apply `entry_config` / `config_overrides` onto the constructed `StrategyConfig` subclass (today `strategies/validate.py` validates overrides via `CreditEntryConfig.from_overrides()` but `loader.py` **never applies them** to the running config — this is an existing gap that must be closed so strategy-specific overrides, e.g. Skyline's strike offsets in §8.2, actually take effect and aren't silently ignored).
3. Require **both** `enabled: true` and `confirmed: true` before instantiating any strategy that declares itself `paper: true` at the class-default level (i.e. strategies that ship with `paper` hard-pinned in their own `Config` dataclass, like `SkylineConfig`, §8.4) — a config-file-only, single-source-of-truth replacement for the earlier idea of a separate `.env` flag (rejected per operator feedback — `.env` is for deployment/environment concerns like credentials and broker selection, not per-strategy business toggles; see §8.4 for the full rationale).

### 3.2 Broker factory: per-strategy shared broker, not one process-global broker

`common/broker_factory.py` today caches exactly one shared broker keyed on `(BROKER, paper, pid)`. Extend it with a strategy-scoped accessor that keys on `(BROKER, paper, strategy_name, pid)`, so MEIC's live shared broker and Skyline's paper shared broker coexist without evicting each other:

```python
def get_shared_broker_for_strategy(strategy_name: str, *, paper: bool, timeout_sec: float | None = None):
    """One cached broker per (strategy_name, paper) pair per process."""
```

Internally this is the existing `get_shared_broker()` machinery generalized from a single `_shared_broker` global to a `Dict[key, BrokerState]`. `get_broker()` itself is unchanged (it already takes an explicit `paper` argument per call). Existing callers of `get_shared_broker()` (MEIC/stop_monitor) keep calling it exactly as today (default key omits `strategy_name`, preserving current behavior) — this is additive, not a breaking change to the live path.

### 3.3 Hard safety rail — a `paper: true` strategy cannot self-promote to live

Configuration mistakes must fail closed. Two independent layers:

1. **Startup validation** (`strategies/validate.py`): if a standalone strategy's `StrategyConfig.paper` is `True`, `validate_strategy_entry()` must also assert `TASTYWARE_API_KEY` is set (the paper session cannot even be constructed otherwise — same check `create_tastytrade_session` already does at runtime, moved to startup so misconfiguration is caught before market open, not on first order attempt).
2. **Runtime assertion in the order path**: the standalone strategy runner (§4.2) resolves its broker via `get_shared_broker_for_strategy(name, paper=config.paper)` and, immediately before *every* order placement (including manual close actions, §6.5), asserts `broker._is_paper is True` when `config.paper` is `True` (see `TastyTradeBroker._is_paper`, `brokers/tastytrade_broker.py:356`, already set from `hasattr(session, 'api_key')`). If the assertion fails, the order is not sent, a `CRITICAL` is logged, and the strategy is halted for the session (fail closed, no retry-into-live).

This means: even if an operator flips global `PAPER_MODE=false`, sets `BROKER=tastytrade` for a live run, and forgets to check the strategy's own `paper:` flag, Skyline (and any future strategy with `paper: true`) still cannot place a live order — it would either fail startup validation or trip the runtime assertion and halt.

### 3.4 Per-strategy trade storage + "PAPER" visibility

`common/trades_layout.py`'s two-name branching is generalized to accept any registered strategy name while keeping today's two constants as the default branch (no behavior change for MEIC/Manual):

```python
def active_dir_for_strategy(strategy: str, root: str | None = None) -> str:
    return os.path.join(root or project_root(), 'trades', 'active', strategy)

def history_dir_for_strategy(strategy: str, root: str | None = None) -> str:
    return os.path.join(root or project_root(), 'trades', 'history', strategy)
```

(`STRATEGY_MEIC = 'MEIC_IC'` and `STRATEGY_MANUAL = 'MANUAL_SPREAD'` already resolve to `trades/active/MEIC_IC` / `trades/active/MANUAL_SPREAD` under this scheme, so the generalized function is a drop-in — confirm via a regression test that existing paths are byte-identical before removing the special-cased branches.)

Every trade JSON written by a strategy carries an explicit `"paper": true|false` field (sourced from `StrategyConfig.paper`, not inferred). Dashboard trade lists render a **PAPER** badge next to any trade where this field is `true`, and paper trades are **excluded by default** from any live PnL/"today's realized" aggregate unless an explicit `?include_paper=1` (or equivalent toggle) is set — paper P/L must never silently blend into live P/L totals.

`dashboard/server.py`'s hard-coded `STRATEGY_MEIC`/`STRATEGY_MANUAL` branches (PnL keys, history glob, session-row endpoints) are **not** rewritten in v1 for every code path — only what's needed to list and inspect Skyline trades and expose its manual close controls (§8.8). Full generic-loop dashboard refactor is a follow-on, tracked as a non-blocking cleanup item (§10).

---

## 4. Part B — Standalone strategy contract (generalizing `IronFlyStrategy`)

Two supported strategy shapes going forward:

| Shape | Examples | Entry mechanism | Exit mechanism | Storage |
|---|---|---|---|---|
| **Tranche / session-CSV** (existing, unchanged) | MEIC_IC, MANUAL_SPREAD | `EntryMonitorRunner` reads session CSV row | `stop_monitor` phases | `trades/{active,history}/<NAME>/*.json`, one row per slot_key |
| **Standalone** (new, this spec) | SKYLINE_SPX (§8), and the dormant IRON_FLY_SPX once revived | Strategy's own `run()` loop, own schedule/entry-window check | Strategy-owned (may be "none" for defined-risk hold-to-expiry, plus manual close, §6.5) | `trades/{active,history}/<NAME>/*.json`, one file per structure/day |

### 4.1 When to use "standalone"

Use the standalone shape when a strategy is **not** a single short/long credit vertical, or needs its own entry/exit cadence that doesn't fit "one session CSV row per slot per side". Skyline (4 independent butterflies, single daily entry, no active exit) is the motivating case.

### 4.2 `StandaloneStrategyRunner` — new shared harness

Rather than have each standalone strategy hand-roll its own `asyncio.run()` loop (as `IronFlyStrategy.run()` does today), add one shared harness in `common/standalone_runner.py` that every standalone strategy reuses:

```python
class StandaloneStrategyRunner:
    """Supervises one standalone BaseStrategy: broker lifecycle, daily state,
    crash-safe resume, config-override merge, and a uniform get_status()
    surface for the dashboard.
    """
    def __init__(self, strategy: BaseStrategy, *, root: str, logger=None): ...
    def start(self) -> None: ...   # spawns supervised thread; resolves broker via
                                    # get_shared_broker_for_strategy(name, paper=config.paper)
    def stop(self) -> None: ...    # cooperative stop() on the wrapped strategy
    def status(self) -> dict: ...  # for dashboard / health endpoint
    def apply_daily_overrides(self) -> None: ...  # merge today's dashboard overrides (§7.3)
```

Responsibilities the harness owns (so individual strategies don't reimplement them):

1. Resolve broker via `get_shared_broker_for_strategy` (never `get_broker()` directly from strategy code — keeps the paper/live boundary in one place, §3.3).
2. Idempotent daily state file: `trades/active/<NAME>/session_<date>.json` recording whether today's entry has already fired, so a launcher restart mid-day does not double-enter. This file also holds the daily override merge target (§7.3) — one file, two concerns, both crash-safe via the existing tempfile+`os.replace` write pattern already used by `SessionPlan.save()`.
3. Crash-safe resume: on restart, if today's state file shows `entered=true`, skip re-entry; if a structure is still open, re-attach for status/settlement/manual-close purposes only (never re-place entry orders).
4. Uniform `run.py` wiring: `run.py` starts one `StandaloneStrategyRunner` per enabled standalone strategy from `load_enabled_strategies()`, alongside (not instead of) the existing MEIC session-CSV path — this is additive to `run.py`, not a replacement of the tranche orchestration.
5. Health surface consumed by `dashboard/server.py` (`status()` → running/last-entry-attempt/last-error/open-structures), independent of the MEIC/Manual PnL tables in §3.4.
6. Applies today's dashboard overrides (§7.3) on top of the yaml-loaded config, both at startup and whenever the dashboard writes a new override (poll or event-driven — implementation detail, not user-visible either way).

### 4.3 New broker primitive: N-leg combo orders (open **and** close)

Add to `brokers/base.py` (abstract) and `brokers/tastytrade_broker.py` (implementation, inherited for free by `brokers/paper_broker.py`):

```python
@dataclass
class ComboLeg:
    symbol: str          # OCC/streamer symbol, broker-agnostic (converted internally)
    action: str          # BUY_TO_OPEN | SELL_TO_OPEN | BUY_TO_CLOSE | SELL_TO_CLOSE
    quantity: int        # positive; ratio encoded here (e.g. 2 for the short body of a fly)

def place_multi_leg_order(
    self,
    legs: List[ComboLeg],
    *,
    price: float,          # net debit (negative) or credit (positive); see _signed_order_price
    order_type: str = 'LIMIT',
    time_in_force: str = 'DAY',
) -> OrderResult:
    ...
```

Implementation notes (`TastyTradeBroker`):

- Builds one `NewOrder` with `legs=[opt.build_leg(Decimal(leg.quantity), OrderAction[leg.action]) for leg in legs]` — this is a straightforward generalization of the existing 2-leg `place_spread_order` (`brokers/tastytrade_broker.py:517`), reusing `_get_option`, `_round_option_price`, and the existing `_retry_on_transient` / new-risk-gate / cooldown wiring verbatim.
- **Enforce the 4-leg platform ceiling explicitly** (§1.3): `place_multi_leg_order` raises `ValueError` if `len(legs) > 4` rather than silently submitting an order the broker will reject — fail fast with a clear message instead of a confusing broker-side rejection.
- **Closing a combo reuses the same primitive** with reversed actions (`BUY_TO_OPEN` ↔ `SELL_TO_CLOSE`, `SELL_TO_OPEN` ↔ `BUY_TO_CLOSE`) and quantities matching the filled quantities on the open order — this is how manual close (§6.5) is implemented; no separate "close" method is needed on the broker.
- `_order_result_from_placed_order` currently assumes exactly the 2-leg short/long shape when computing `spread_credit` (`short_filled`/`long_filled` bucketing). For N-leg orders this must fall back to `order_limit_price` as the fill-price source (`filled_price_source='order_limit_fallback'`, already an existing code path) rather than attempting per-leg netting — sufficient for Skyline since v1 has no active management that needs a precise realized-open credit, only fill/no-fill and quantity, plus settlement math computed from strikes (§8.6), not from fill-price bookkeeping.
- Respects the existing new-risk REST gate (`common.trading_gate.effective_new_risk_blocked`) exactly like `place_spread_order` for **opening** legs — a standalone strategy is not exempt from the account-wide entry gate. Closing legs (manual close) are **not** gated by the new-risk gate (closing risk is never something we want to block — mirrors how `place_spread_close_order` already behaves for MEIC emergency closes).
- `MockBroker` (test double, wherever it lives for `tests/test_v3_*`) gets a `place_multi_leg_order` stub returning a synthetic filled `OrderResult` for unit tests.

### 4.4 Minimal checklist for adding a new strategy (either shape)

1. `strategies/<name>/strategy.py` — `<Name>Config(StrategyConfig)` + `<Name>Strategy(BaseStrategy)` implementing `run()`/`stop()`/`get_status()` (+ `schedule()` returning `[]` if standalone, non-empty `TrancheSlot`s if tranche-shaped).
2. Register in `config/strategies.yaml` (`enabled`, `module`, `broker`, `instrument`, `stop_profile`/`exit_profile`, `paper`, `scheduled`, `confirmed` if the strategy defaults `paper: true` in its own config class).
3. If standalone: wire into `run.py` via `StandaloneStrategyRunner` (§4.2), not `EntryMonitorRunner`.
4. If it needs a new order shape: extend `brokers/base.py`/`brokers/tastytrade_broker.py` (§4.3) rather than hand-building SDK calls inside the strategy — strategy code should never import `tastytrade.order` directly (mirrors V4's Rule 3 for good reason, applied pragmatically here — see §1.7).
5. Add/extend an Exit Condition profile (existing phase-based, or `hold_to_expiry` + manual close, §6).
6. `strategies/validate.py` gains a branch so standalone strategies aren't forced through `CreditEntryConfig` validation (today `validate_strategy_entry()` unconditionally calls `CreditEntryConfig.from_overrides(...)`, which is meaningless for a non-credit-spread strategy).
7. Add `tests/test_strategy_loader.py` / `tests/test_strategy_validation.py` cases for the new entry (mirrors existing MEIC/Manual assertions).
8. If the strategy exposes any dashboard-editable setting, wire it through the generic daily-override endpoint (§7.4) — do not add a strategy-specific override endpoint.

---

## 5. Part C — FOMC-day fix: keep recording prices when the market is open but we're not trading; real holidays are unchanged

### 5.1 The fix

Add a small, standalone helper that answers a narrower question than `check_trading_day()` — "is the exchange itself open today?" — reusing only the NYSE holiday calendar it already fetches, with no FOMC logic involved:

```python
def is_market_closed_today() -> bool:
    """True only for a real NYSE holiday. Deliberately ignores FOMC days —
    the exchange is open and SPX trades normally on FOMC days."""
    today = _central_now()
    today_str = today.strftime('%y%m%d')
    market_closed = set(common_config._get_nyse_holidays(today.year))
    return today_str in market_closed
```

`run.py:main()` checks this **first**. Only a real holiday keeps today's early-return behavior; an FOMC/day-after-FOMC day now falls through to start data collection before being gated out of strategy trading:

```python
# AFTER
if not force and is_market_closed_today():
    # Real NYSE holiday: exchange is shut, no quotes exist, nothing to record.
    # Unchanged from today's behavior — do not start anything.
    reason = 'NYSE is CLOSED today. No trading, no data.'
    terminal_info(log, reason)
    _write_status('skipped', reason)
    return

should_trade, reason = True, 'forced'
if not force:
    should_trade, reason = check_trading_day()
    if not should_trade:
        log.info('Trading disabled today (%s) — market is open, price recording continues.', reason)

# Market is open (normal day, OR an FOMC/day-after-FOMC day) -> always collect data.
streamer = start_streamer()
market_data = start_market_data_recorder()
stop_mon = None if no_stop_monitor else start_stop_monitor(paper=paper)

if should_trade:
    strategies = load_enabled_strategies()
    # ... existing tranche orchestration + StandaloneStrategyRunner.start() calls ...
else:
    _write_status('skipped_trading_only', reason)
    # No strategies loaded/started. streamer + market_data + stop_monitor
    # (for any already-open positions from a prior day, if applicable) still run.
```

Net effect, per day type:

| Day type | `is_market_closed_today()` | Streamer / market_data | Strategies |
|---|---|---|---|
| Normal trading day | `False` | Runs (unchanged) | Run (unchanged) |
| FOMC day | `False` | **Now runs** (was: skipped — the bug) | Skipped (unchanged) |
| Day after FOMC | `False` | **Now runs** (was: skipped — the bug) | Skipped (unchanged) |
| Real NYSE holiday | `True` | Skipped (unchanged) | Skipped (unchanged) |

This is a **narrow, surgical fix** targeting exactly the FOMC/day-after-FOMC rows: it does not touch `check_trading_day()`'s logic or the holiday calendar at all, does not change holiday behavior (the case the operator flagged — "no point running anything" on a day the market never opens), and does not change behavior on a normal trading day (`should_trade` is `True`, so the code path is identical to today).

### 5.2 Defense in depth for standalone strategies

`StandaloneStrategyRunner.start()` additionally re-checks `check_trading_day()` (or an equivalent injected check) itself before allowing its wrapped strategy's `run()` to attempt entry — this matters if `run.py` is ever invoked with `--force` for manual/off-hours testing, or if a future caller starts a `StandaloneStrategyRunner` outside `run.py`'s normal flow. This mirrors the existing pattern where the new-risk gate is checked at multiple layers rather than trusted to a single call site. This check is strictly about **trading**, not data recording — it never calls `is_market_closed_today()`.

### 5.3 File touch (this part only)

| File | Change |
|---|---|
| `run.py` | Add `is_market_closed_today()`; reorder `main()` so only a real NYSE holiday short-circuits before streamer/market_data/stop_monitor start — FOMC/day-after-FOMC days start data collection but still skip strategy loading |
| `common/standalone_runner.py` (new, shared with Part B) | Defense-in-depth trading-day check before `run()` |
| `tests/test_run_one_day.py` | The existing `test_holiday_skips_trading` test (line 66) mocks `check_trading_day` returning `(False, 'FOMC')` and asserts the streamer is **not** called — that assertion is the exact conflation this section fixes, and is itself evidence of the bug encoded as a passing test. Split it into a real-holiday case (streamer not called, unchanged) and a separate FOMC-day case (streamer **is** called; strategies are not) |

### 5.4 Tests (this part only)

| # | Test |
|---|---|
| C1 | Real NYSE holiday (`is_market_closed_today()` → `True`): `start_streamer()`/`start_market_data_recorder()` are **not** called, `main()` returns immediately — unchanged from today |
| C2 | FOMC day (`is_market_closed_today()` → `False`, `check_trading_day()` → `(False, 'FOMC...')`): `start_market_data_recorder()` **is** called |
| C3 | Same FOMC day: `load_enabled_strategies()` / tranche orchestration / `StandaloneStrategyRunner.start()` are **not** called |
| C4 | Day after FOMC: same as C2/C3 (data recorded, strategies skipped) |
| C5 | Normal trading day: behavior is byte-identical to today (no regression) |
| C6 | `StandaloneStrategyRunner` refuses to enter if its own trading-day check fails, even if started directly (bypassing `run.py`'s gate) |

---

## 6. Part D — Exit Conditions framework (extends `StopProfile`, does not replace it)

This section directly implements the operator's feedback: generalize "stop rules" into a broader, per-strategy-configurable **Exit Conditions** concept, without repeating the V4 rewrite mistake (§1.7). Every subsection below is additive to `blocks/stop/*` — no existing class is renamed, no existing method signature changes, and no existing strategy's behavior changes unless its yaml entry explicitly opts in.

### 6.1 Naming: "Exit Conditions" is a config-facing concept, not a code rename

- `config/strategies.yaml` accepts a new key `exit_profile` as a **synonym** for the existing `stop_profile` key (both resolve through the same `resolve_stop_profile()` / `_PROFILE_REGISTRY` in `blocks/stop/stop_profile.py`). `stop_profile` continues to work unchanged for MEIC/Manual — this is purely additive vocabulary for new strategies and documentation, so operators can write `exit_profile: skyline_hold_to_expiry` instead of `stop_profile: skyline_hold_to_expiry` if they prefer the clearer name. No code path needs to know which spelling was used past config parsing.
- Internally, the classes stay `StopProfile`, `PhaseBase`, `_PROFILE_REGISTRY`, `register_stop_profile()` — renaming these would touch every existing profile/phase/import across `blocks/stop/`, `strategies/meic/strategy.py`, `strategies/manual_spread/strategy.py`, and multiple tests, for zero functional benefit. This is exactly the kind of low-value, high-risk rename V4 attempted broadly; here it is scoped to a single new yaml alias.

### 6.2 New phase: Profit Target

New file `blocks/stop/phases_profit_target.py`:

```python
class PhaseProfitTarget(PhaseBase):
    """Close the position once its mark reaches a configured profit target.

    Works for both credit and debit spreads via `spread_type`-aware comparison:
    - credit: exit when current spread mid <= (entry_credit * (1 - target_pct))
    - debit:  exit when current spread mid >= (entry_debit  * (1 + target_pct))
    """
    priority = 5   # evaluated before Phase1InitialStop so a profit exit
                   # pre-empts a stop-loss check on the same tick
    name = 'phase_profit_target'

    def should_activate(self, monitor) -> bool:
        state = monitor.state
        return (
            state.get('status') == 'open'
            and (state.get('exit_config') or {}).get('profit_target_pct') is not None
        )

    def _evaluate_active(self, monitor) -> str:
        return PhaseAction.EXIT_REQUIRED if self._target_hit(monitor) else PhaseAction.NONE

    def _target_hit(self, monitor) -> bool: ...   # per spread_type math above

    def execute(self, monitor) -> None:
        monitor.replace_with_limit_close(reason='profit_target_hit')
```

- `profit_target_pct` lives on the trade's `exit_config` section (mirrors how `stop_multiplier` already lives on the MEIC session-CSV row today, `blocks/session/plan.py:SessionRow.stop_multiplier`) — set per strategy in yaml `config_overrides`, or per-row for MEIC/Manual if an operator later chooses to add a `profit_target_pct` session-CSV column (not required for this document; MEIC/Manual continue with zero profit target by default, i.e. `should_activate` returns `False`, fully inert).
- Reuses `monitor.replace_with_limit_close()` — the exact same closing mechanism `Phase1InitialStop` already uses — so no new order-placement code is needed for this phase.
- Registered into new profiles (§6.6) that opt in; **never added to `meic_stop_profile()`'s phase list**, so MEIC/Manual are completely unaffected unless a human explicitly creates and assigns a new profile that includes it.

### 6.3 Debit-side support (`spread_type='debit'`)

`StopProfile.spread_type` already has a `'credit' | 'debit'` type hint with "debit deferred" noted (`blocks/stop/stop_profile.py:16`). Implement the debit branch now, scoped narrowly:

- `blocks/stop/breach.py`'s `spread_mark_price` / `spread_breach_triggered` (the default `breach_calc`/`breach_condition` on `StopProfile`) get a `spread_type` parameter; for `'debit'`, breach direction and profit-target direction invert relative to `'credit'` (a debit spread's value needs to **rise** to be profitable, unlike a credit spread's value needing to **fall**).
- No existing profile's `spread_type` changes — `meic_credit_spread` and `manual`'s stay `'credit'`. This only unblocks a *future* debit-spread strategy (not built in this document) from registering `spread_type='debit'` without hitting a `NotImplementedError`-shaped dead end.
- Included here (rather than deferred again) because Skyline's butterflies are debit trades (paying a net debit to open a long butterfly) and, even though Skyline uses `hold_to_expiry` with no active phases (§6.7), the settlement math in §8.6 reuses the same debit-aware profit/loss convention for consistency — better to define it once, correctly, than to have two divergent debit-P/L formulas in the codebase.

### 6.4 Strategy-wide loss cap ("portfolio guard")

New module `common/portfolio_guard.py`:

```python
@dataclass
class PortfolioGuardConfig:
    strategy_name: str
    max_daily_loss: float | None = None   # e.g. 2000.0; None = disabled (default)
    action: str = 'halt_and_close'        # 'halt_and_close' | 'halt_only'

class PortfolioGuard:
    """Aggregates realized + unrealized P/L across one strategy's open + closed-today
    positions and, if `max_daily_loss` is breached, halts new entries and (optionally)
    closes all open positions for that strategy.
    """
    def __init__(self, config: PortfolioGuardConfig, *, root: str): ...
    def check(self) -> 'GuardStatus':
        """Read today's trade JSONs under trades/active/<name>/ and
        trades/history/<name>/ for today's date, sum realized + mark-to-market
        unrealized P/L, compare to max_daily_loss."""
    def halt(self) -> None:
        """Write trades/active/<name>/halted_<date>.json — read by both
        EntryMonitorRunner (for tranche strategies) and StandaloneStrategyRunner
        (for standalone strategies) as an additional entry-gate check, and by
        stop_monitor/manual-close (for 'halt_and_close') to trigger closes."""
```

Integration points (both additive):

- `blocks/entry/runner.py:EntryMonitorRunner` gains one extra check alongside its existing pause/skip/window checks: if `trades/active/<strategy>/halted_<date>.json` exists, treat the row as paused for the rest of the day (same code path as an operator-set `paused=True`, just triggered by the guard instead of a dashboard click).
- `common/standalone_runner.py:StandaloneStrategyRunner` checks the same halt file before calling the wrapped strategy's `run()`/entry logic.
- For `action='halt_and_close'`: the guard additionally triggers each open position's existing close mechanism — `place_spread_close_order` for tranche-shaped strategies (MEIC/Manual, if ever opted in), or the manual multi-leg close (§6.5) for standalone strategies.
- **This directly answers the operator's example** ("if we hit a loss of $2,000 it will trigger a stop on the whole strategy and close everything and not open new legs, this will be good for MEIC"): MEIC can opt in by setting `max_daily_loss: 2000` in its yaml `config_overrides` — the guard is generic and works for any strategy name, tranche-shaped or standalone, because it only reads/writes files keyed by strategy name and delegates the actual close action to each strategy's existing close mechanism, rather than needing to understand each strategy's position shape itself.
- **Explicitly not enabled for MEIC by default** in this document (§2 non-goals) — the capability is built and tested, but turning it on for the live MEIC strategy is a separate operator decision outside this spec's scope, made by editing `config/strategies.yaml` (or a daily override, §7) when ready.

### 6.5 Manual close action (generic, not Skyline-specific)

New shared function in `common/manual_close.py`:

```python
def close_structure(strategy_name: str, structure_id: str, *, root: str) -> CloseResult:
    """Load the open trade JSON for (strategy_name, structure_id), build reversed
    ComboLeg actions from its recorded legs at their filled quantities, submit via
    place_multi_leg_order, and update the trade JSON status to 'closing' -> 'closed'
    once confirmed. Works for any N-leg (<=4) structure, not just butterflies."""

def close_all_structures(strategy_name: str, *, root: str) -> List[CloseResult]:
    """Iterate all open structures for a strategy today and close_structure() each."""
```

- For Skyline, `structure_id` is the butterfly name (`BF1`..`BF4`) from the trade JSON's `structures[]` array (§8.7).
- This is deliberately **generic across strategies** (keyed by `strategy_name` + `structure_id`, reading whatever shape of legs is recorded in that strategy's trade JSON) so it is not Skyline-specific plumbing — any future standalone strategy gets manual close for free.
- Manual close is **operator-triggered only** (dashboard button, §8.8, or a CLI script for scripted/emergency use) — never called automatically by any phase or guard in v1, except by the Portfolio Guard's `halt_and_close` action (§6.4), which is itself only active when an operator has explicitly configured `max_daily_loss` for that strategy.
- Logged distinctly (`MANUAL_CLOSE_REQUESTED` / `MANUAL_CLOSE_FILLED`) from any automated exit, so trade history can distinguish "the software decided to close this" from "an operator clicked close."

### 6.6 New Exit Condition profile registrations

`blocks/stop/profiles/skyline.py` (new) registers:

```python
SKYLINE_HOLD_TO_EXPIRY = 'skyline_hold_to_expiry'

def skyline_hold_to_expiry_profile() -> StopProfile:
    return StopProfile(
        name=SKYLINE_HOLD_TO_EXPIRY,
        spread_type='debit',
        phases=[],                 # no automated phases at all — see §6.7
        long_close_delay_sec=0,
    )

register_stop_profile(SKYLINE_HOLD_TO_EXPIRY, skyline_hold_to_expiry_profile)
```

A separate, reusable `hold_to_expiry_profile(name, *, spread_type='credit')` factory also lives in `blocks/stop/profiles/hold_to_expiry.py` for any *other* future defined-risk strategy that wants the same "monitor only, no automated phases" behavior without duplicating Skyline's specific registration.

### 6.7 `stop_monitor` treatment of an empty-phases profile (monitor-only)

`stop_monitor` must treat `phases=[]` as **monitor-only**: it still tracks the position for dashboard P/L display and logs a settlement check at/after 4:00pm ET, but never constructs or submits an automatic closing order. This is a small, additive change to `blocks/stop/monitor.py`/`blocks/stop/runner.py` — gate any "evaluate phases and act on `EXIT_REQUIRED`" branch on `bool(profile.phases)`; when empty, the monitor loop degrades to "refresh marks, update `unrealized_pnl` in the trade JSON, watch for manual close requests (§6.5), watch for expiry/settlement (§8.6)." Strategies using the tranche shape (MEIC/Manual) always have non-empty phases today, so this is inert for them.

Standalone strategies may also simply **not attach to `stop_monitor` at all**; the profile above exists for any future standalone strategy that still wants dashboard-visible tracked-P/L and manual-close support without writing its own monitoring loop.

### 6.8 File touch list (this part only)

| File | Change |
|---|---|
| `blocks/stop/phases_profit_target.py` (new) | `PhaseProfitTarget` |
| `blocks/stop/breach.py` | Add `spread_type` parameter to `spread_mark_price`/`spread_breach_triggered` for debit support |
| `common/portfolio_guard.py` (new) | `PortfolioGuardConfig`, `PortfolioGuard` |
| `common/manual_close.py` (new) | `close_structure`, `close_all_structures` |
| `blocks/stop/profiles/hold_to_expiry.py` (new) | Reusable monitor-only profile factory |
| `blocks/stop/profiles/skyline.py` (new) | Registers `skyline_hold_to_expiry` |
| `blocks/stop/monitor.py` / `blocks/stop/runner.py` | Gate automated-close branch on `bool(profile.phases)`; still refresh marks/settlement when empty |
| `blocks/entry/runner.py` | Extra halt-file check alongside existing pause/skip checks (inert unless a guard is configured) |
| `strategies/loader.py` | Accept `exit_profile` as a yaml synonym for `stop_profile` |

### 6.9 Tests (this part only)

| # | Test |
|---|---|
| D1 | `PhaseProfitTarget` triggers `EXIT_REQUIRED` at the configured credit-side target; does not trigger for a strategy with no `profit_target_pct` set |
| D2 | `PhaseProfitTarget` triggers correctly for `spread_type='debit'` math (inverted direction vs. credit) |
| D3 | `PortfolioGuard.check()` correctly sums realized (from history) + unrealized (from active, mark-to-market) P/L for a strategy |
| D4 | Breaching `max_daily_loss` writes the halt file and both `EntryMonitorRunner` and `StandaloneStrategyRunner` refuse new entries afterward |
| D5 | `action='halt_and_close'` triggers close on all open positions for that strategy; `action='halt_only'` does not |
| D6 | `close_structure` builds correct reversed-action legs at correct quantities and calls `place_multi_leg_order` once |
| D7 | `close_all_structures` closes every open structure for a strategy and skips already-closed ones |
| D8 | `hold_to_expiry` profile (`phases=[]`) never triggers an automated closing-order call in `stop_monitor`, but still updates `unrealized_pnl` in the trade JSON |
| D9 | `exit_profile` and `stop_profile` yaml keys both resolve to the same registered profile |
| D10 | Existing MEIC/Manual stop-runner tests (`tests/test_stop_runner_gate.py`, `tests/test_stop_monitor_engine.py`, etc.) stay green — zero behavior change for profiles that don't opt into new phases |

---

## 7. Part E — Daily dashboard overrides (generalizing MEIC's session-CSV pattern)

### 7.1 Precedence model

```text
1. Dataclass field default (code)              <- lowest precedence
2. config/strategies.yaml entry (permanent)    <- operator's standing default
3. Today's override file (dashboard-editable)  <- highest precedence, today only
```

`config/strategies.yaml` is **only ever hand-edited by the operator** and represents "what happens starting tomorrow if untouched." The dashboard never writes to it. Any dashboard-driven change for the current session goes into a per-strategy, per-day override file instead.

### 7.2 Precedent already in the codebase

This generalizes what MEIC's session CSV already does: `blocks/session/bootstrap.py:_default_rows()` seeds each day's session CSV from code/config defaults (`MEIC_DEFAULT_PAUSED_LOTS`, `meic_config.QUANTITY`, etc.); the dashboard then edits `paused`/`skip`/`quantity`/etc. **in that day's CSV only** via `SessionPlan.update_row()`; `bootstrap_meic_session_if_missing()` only creates a new file if one doesn't already exist for the date, so each new trading day starts back at the coded/yaml defaults. This section makes the same three-layer precedence available to **any** strategy, not just MEIC's session-CSV rows.

### 7.3 Storage: reuse the standalone daily state file, add a generic key for tranche strategies

- **Standalone strategies** (Skyline, future ones): the same `trades/active/<NAME>/session_<date>.json` from §4.2 gains an `overrides` object:

  ```json
  {
    "entered": false,
    "skip_reason": null,
    "overrides": {
      "enabled": false,
      "max_daily_loss": 1500.0
    }
  }
  ```

  `StandaloneStrategyRunner.apply_daily_overrides()` merges this object on top of the yaml-loaded `StrategyConfig` at startup and whenever the dashboard posts a change (§7.4). Only keys present in `overrides` are changed; everything else stays at the yaml/code default.

- **Tranche strategies** (MEIC/Manual): **no change** — they keep using the existing session-CSV row fields (`paused`, `skip`, `quantity`, `stop_multiplier`, etc.) exactly as today. This section does not migrate MEIC/Manual onto the new file-based override format; it only generalizes the *pattern* (permanent config vs. daily override) to strategies that don't have a session CSV.

### 7.4 Generic dashboard API (not per-strategy endpoints)

```text
GET  /api/strategy/<name>/config          -> effective config (yaml defaults + today's overrides merged), plus the
                                              yaml-only defaults separately, so the UI can show "default vs. today"
POST /api/strategy/<name>/overrides       -> body: {"key": value, ...}; validated against that strategy's
                                              Config dataclass fields (reject unknown keys, same validation
                                              style as CreditEntryConfig.from_overrides' unknown-key check);
                                              merges into today's override file; takes effect on next
                                              apply_daily_overrides() poll (or immediately if the runner
                                              subscribes to file changes — implementation detail)
DELETE /api/strategy/<name>/overrides/<key>  -> remove one override key, reverting that field to the yaml/code default for the rest of today
```

This is one generic endpoint set for **all** strategies (tranche or standalone) that expose overridable fields, rather than a bespoke endpoint per strategy — satisfies "all new configs should have a way to switch from the dashboard for a day" without hand-building a new API surface for every future strategy.

### 7.5 File touch list (this part only)

| File | Change |
|---|---|
| `common/standalone_runner.py` | `apply_daily_overrides()`, override-merge logic |
| `dashboard/server.py` | New generic `GET/POST/DELETE /api/strategy/<name>/overrides` routes |
| `strategies/loader.py` or `common/standalone_runner.py` | Validation reusing the existing "reject unknown override keys" pattern from `CreditEntryConfig.from_overrides` |

### 7.6 Tests (this part only)

| # | Test |
|---|---|
| E1 | Yaml default + no override file → effective config equals yaml default |
| E2 | Override file sets `enabled: false` → strategy does not enter today even though yaml says `enabled: true` |
| E3 | Override written today does not persist to tomorrow (new day → new state file → back to yaml default) |
| E4 | Posting an unknown override key is rejected with a clear error, mirroring `CreditEntryConfig.from_overrides`'s existing behavior |
| E5 | `DELETE` on one override key reverts only that key, leaving other overrides intact |
| E6 | MEIC/Manual session-CSV pause/skip behavior is completely unchanged (this part does not touch `blocks/session/*`) |

---

## 8. Part F — Skyline: multi-butterfly 0DTE SPX strategy (paper-only)

### 8.0 Source and credibility caveat

Structure and entry rule per Sorabh Khurana's "Skyline" 0DTE butterfly strategy as described in [The Skyline 0DTE butterfly strategy explained](https://www.thetaprofits.com/the-skyline-0dte-butterfly-strategy-explained/) (Theta Profits, John Einar Sandvand, Jul 26 2026), backtested by the author in Option Omega (~2 years, $5,000 → ~$74,000, ~300% CAGR reported in the article).

**This backtest result is contested in the article's own comments** — one commenter reports reproducing 2024–2026 results but very poor results for 2022–2024 on the same structure; another reports poor results when replicating with a portfolio of separate strategy backtests rather than the author's combined portfolio view; the author has not published the underlying per-strategy Option Omega config for independent verification at time of writing. **This is exactly why this strategy ships paper-only with no live path in this spec** (§2, non-goals) — v1's job is to observe real fills/theta decay/settlement behavior on a paper account, not to validate the historical edge.

### 8.1 Instrument and timing

- Underlying: **SPX**, 0DTE (same-day expiration), consistent with the rest of this codebase's 0DTE SPX focus.
- Entry time: **9:45 AM America/New_York** (reuse the existing Central-time session-clock helpers, converting; the rest of the codebase operates in `America/Chicago` — `common.session_cleanup.central_today()` / launcher clock — so Skyline's entry check must explicitly use ET internally per the source, not silently reuse the Chicago-anchored MEIC tranche clock).
- Entry gate 1 — **gap-down skip**: skip the entire day if SPX has declined more than **25 points** overnight (prior close → current price at entry-check time). No equivalent filter on overnight rallies (per source — asymmetric, gap-down only). Data source detailed in §8.3.
- Entry gate 2 — **trading-day skip**: skip the entire day if `check_trading_day()` (or the daily-halt-file from a Portfolio Guard, §6.4, if ever configured) says not to trade — real holiday, FOMC day, or day-after-FOMC alike. This never affects price recording: SPX prices are still collected on FOMC/day-after-FOMC days since the market is genuinely open (§5); on a real holiday there is nothing to collect either way.
- Single shot per day: exactly one entry attempt; no re-entry, no scaling, no chasing (unlike MEIC's chase1/chase2 machinery — irrelevant here, v1 either fills near mid or the day is skipped, see §8.5).

### 8.2 Structure (four overlapping 25-wide butterflies, ATM-relative)

All strikes below are **offsets from ATM** (SPX spot rounded to the nearest strike increment at entry time, default nearest 5 to match SPX 0DTE listed strikes) and are **config-driven**, not hardcoded, per the "adding more strategies should not require code changes for tuning" goal (§2). Defaults reproduce the source article exactly:

| Butterfly | Long wing 1 | Short body (×2) | Long wing 2 | Type |
|---|---|---|---|---|
| BF1 | ATM + 0 | ATM + 25 | ATM + 50 | Call |
| BF2 | ATM + 40 | ATM + 60 | ATM + 75 | Call (5-pt "floor" — see note) |
| BF3 | ATM + 5 | ATM − 20 | ATM − 45 | Call (source: "can be call or put; balanced") |
| BF4 | ATM − 35 | ATM − 60 | ATM − 80 | Put (5-pt "floor" — see note) |

Notes:

- BF2 and BF4 are intentionally **not** symmetric 25-wide flies on both sides (BF2 wing widths are 20/15, not 25/25) — the source calls this a 5-point "floor" that trims the outer wing to reduce max loss if price runs well beyond the structure. Implement exactly the four strikes given per butterfly (a config list of 4 `(offset, action, qty)` tuples per butterfly), not a generic "symmetric fly" generator, so the intentional asymmetry is preserved.
- BF3's type (call vs put) is explicitly stated as interchangeable by the source ("balanced" — same payoff either way at same strikes, modulo minor early-exercise/dividend/rate skew irrelevant to 0DTE SPX index options, which are European-style and cash-settled). Default to **call** for consistency with BF1/BF2; make it configurable to `put` for anyone who wants to cross-check pricing/liquidity between the two.
- Quantity: `qty` contracts per structure (default 1, matching the "2 contracts" language in the source's backtest being a scaling choice, not a structural requirement); one net position size for all 4 butterflies (no independent per-butterfly sizing in v1).
- Each butterfly is its own 4-leg ticket — the **platform ceiling of 4 legs per order** (§1.3) makes this the natural boundary, not an arbitrary v1 choice.

### 8.3 Gap-down check: data source (internal CSVs, not an external API)

**Current price:** `broker.get_spx_price()` (MQTT-backed cache), the exact same source MEIC already uses for live SPX reads.

**Prior day's close:** reuse the bot's own recorded price history rather than adding a new external dependency (Yahoo Finance was considered and explicitly rejected — the bot already collects everything needed internally; an external vendor would add a new credentials/rate-limit/availability failure mode for no benefit). Concretely:

1. Extract the existing private helper in `common/expiry_settlement.py` (`_spx_from_market_data(root, for_date, source=...)`, which already reads `data/<date>/SPX_1m.csv` or `SPX_polls.csv` for the last bar at/before 15:00 CT) into a small **public**, reusable function:

   ```python
   # common/expiry_settlement.py
   def resolve_daily_close(root: str, for_date: date) -> Optional[Tuple[float, str]]:
       """Public wrapper: best-available SPX close for for_date, reusing the same
       candidate/priority logic already used for expiry settlement (ohlc_daily >
       spx_polls > spx_1m). Returns (price, source) or None."""
   ```

2. Skyline's entry-window check calls `resolve_daily_close(root, yesterday)` for the **prior trading day** (skip weekends/holidays via the existing NYSE calendar helper already used by `check_trading_day()`, `common.config._get_nyse_holidays`).
3. **Fallback order**, matching the existing settlement priority table (`_SETTLEMENT_SOURCE_PRIORITY`): `ohlc_daily` → `spx_polls` → `spx_1m`. If none exist (e.g. `market_data` recorder was down that day — a gap this document also actively prevents going forward via §5's fix), **do not guess**: skip today's Skyline entry and log `SKYLINE_SKIPPED_NO_PRIOR_CLOSE`, distinct from `SKYLINE_SKIPPED_GAP_DOWN`. Never trade against an unverified reference price.
4. This function is reusable by both `common/expiry_settlement.py`'s existing settlement resolution and Skyline's gap check — one shared implementation, not two divergent ones.

### 8.4 Enablement — single source of truth, two required fields (no `.env` toggle)

Earlier drafts of this spec proposed a separate `.env` flag (`SKYLINE_ENABLED`) as a second independent switch. **Rejected per operator feedback**: `.env` is for deployment/environment-level concerns (broker credentials, which broker/account, host-level toggles) — it should not carry per-strategy business configuration, which belongs entirely in `config/strategies.yaml` where all other strategy behavior already lives and where an operator reviewing "what will run today" only has to look in one place.

Skyline instead requires **both** of the following to be `true` **in the same yaml entry**:

```yaml
- name: SKYLINE_SPX
  enabled: false     # ordinary strategy on/off switch (same field every strategy has)
  confirmed: true    # explicit, additional acknowledgment — new field, only meaningful
                      # for strategies whose Config class hard-pins paper: true
```

`strategies/validate.py` enforces: any strategy whose constructed `StrategyConfig.paper` is `True` **and** which sets a new class-level flag `requires_confirmation = True` (set on `SkylineConfig`, not a generic default) must also have `confirmed: true` in its yaml entry, or `validate_startup_config()` raises `StrategyConfigError` at startup — the strategy fails to load rather than silently staying disabled, so a misconfiguration is visible immediately in logs rather than looking like "it's just off." This preserves "hard to turn on by accident" (two fields, both explicit, both in the one file an operator already reviews) without spreading strategy configuration across two files (`.env` + yaml), addressing the operator's note directly.

Daily override note (§7): `enabled` can still be flipped off for **today only** via the dashboard (§7.4) without touching the yaml — e.g. an operator can pause Skyline for one session without editing `confirmed`/`enabled` in the permanent file. `confirmed` itself is **not** overridable via the daily-override mechanism (it must be a deliberate, permanent yaml edit) — enforced by excluding it from the allow-list of override-able keys in §7.4's validation.

### 8.5 Entry flow

1. At (or first tick after) 9:45 AM ET, `SkylineStrategy` (standalone, §4) checks `check_trading_day()` (§8.1 gate 2) and the halt file from any configured Portfolio Guard (§6.4) — if either blocks, skip with the appropriate `skip_reason` and stop.
2. Fetch current SPX spot via `get_spx_price()` and prior close via `resolve_daily_close()` (§8.3). If prior close is unavailable, skip with `skip_reason='no_prior_close'`.
3. If `spot - prior_close < -gap_down_skip_points` (default 25), log `SKYLINE_SKIPPED_GAP_DOWN` and mark today's state file `entered=false, skip_reason='gap_down'` — no orders placed, no retry later in the day.
4. Otherwise, round spot to `atm_rounding` (default 5) to get ATM, resolve the 0DTE SPX option chain (same chain-fetch path `TastyTradeBroker._get_option`/`get_option_chain` already uses), and compute the 16 leg symbols from the config table (§8.2).
5. For each of the 4 butterflies independently: submit one `place_multi_leg_order` (§4.3) — 4 legs, net price = mid of the combo at submission (per `entry_price_style`), `order_type='LIMIT'`, `time_in_force='DAY'`.
6. Wait up to `entry_fill_wait_sec` (default 20s) per butterfly for a fill; unlike MEIC there is **no chase ladder** — if a butterfly does not fill within the wait window, cancel it, log `SKYLINE_LEG_UNFILLED`, and leave the other butterflies as-is (partial-structure day is recorded and visible on the dashboard, not silently treated as a full success). This asymmetric-fill risk is called out explicitly as a known v1 limitation (§9).
7. On completion (full, partial, or skipped), write one trade JSON to `trades/active/SKYLINE_SPX/skyline_<date>.json` (schema §8.7) and mark the daily state file `entered=true`.

### 8.6 Exit: no automated stop, hold to expiration, manual close available

Per the user requirement ("this won't have any stop in it as it is risk defined") and the source's own backtest methodology ("simply holds every position until expiration... no routine stop losses, adjustments or early exits") — while also giving the operator a way to intervene by hand:

- Skyline uses the `skyline_hold_to_expiry` profile (§6.6) — **zero automated phases**. `stop_monitor` still tracks marks and updates `unrealized_pnl` in the trade JSON for dashboard visibility (§6.7), but never submits a closing order on its own.
- Max loss is bounded by the combo structure itself (defined-risk butterflies — worst case per butterfly is the net debit paid, or a bounded value based on the wing-floor construction for BF2/BF4); no software stop is needed, consistent with the source's design intent (Capital-at-Risk framing, §8.0).
- **Manual close is available** (§6.5): "Close All" closes all 4 butterflies; "Close [BF1..BF4]" closes one. Both are operator-triggered only from the dashboard (§8.8), logged as `MANUAL_CLOSE_*`, and never triggered automatically — unless an operator has separately opted Skyline into a Portfolio Guard (§6.4) with `action='halt_and_close'`, which is off by default.
- **No discretionary intraday adjustments** (§2 non-goal) — the "add a call condor to lift the valley" / "add another butterfly on a breakout" tactics mentioned by the source's author are explicitly not implemented in v1.
- At/after market close, SPX cash-settles automatically; a lightweight EOD reconciliation (reuse the same `resolve_daily_close()` helper from §8.3, called for *today's* date after 4:00pm ET instead of *yesterday's*) computes final settlement P/L per butterfly from strikes vs. the settlement price (using the debit-aware math from §6.3) and marks the structure closed in `trades/history/SKYLINE_SPX/`.

### 8.7 Trade JSON schema (new — multi-leg structure, not a vertical)

```json
{
  "strategy": "SKYLINE_SPX",
  "paper": true,
  "date": "2026-07-28",
  "status": "open",
  "entry_time_et": "09:45:03",
  "atm": 6400,
  "quantity": 1,
  "skip_reason": null,
  "structures": [
    {
      "name": "BF1",
      "legs": [
        {"symbol": ".SPXW260728C6400", "action": "BUY_TO_OPEN", "qty": 1, "fill_price": null},
        {"symbol": ".SPXW260728C6425", "action": "SELL_TO_OPEN", "qty": 2, "fill_price": null},
        {"symbol": ".SPXW260728C6450", "action": "BUY_TO_OPEN", "qty": 1, "fill_price": null}
      ],
      "order_id": "...",
      "status": "filled",
      "net_debit": 3.15,
      "unrealized_pnl": null,
      "closed_manually": false
    }
  ],
  "net_debit_total": 0.0,
  "capital_at_risk": 0.0,
  "unrealized_pnl_total": 0.0,
  "settlement": null
}
```

`unrealized_pnl` (per structure) and `unrealized_pnl_total` are refreshed by `stop_monitor`'s monitor-only loop (§6.7) using current option mids (`fetch_option_mids_api`). `closed_manually` distinguishes an operator-triggered close (§6.5) from a hold-to-settlement outcome in history/reporting.

Follows the existing `blocks/stop/state.py`-style JSON-state convention (status field, one file per structure-day, atomic write via the same tempfile+`os.replace` pattern as `SessionPlan.save()`) but with a `structures[]` array instead of the flat short/long fields used by the vertical-spread schema — this is a **new**, strategy-specific shape (not shoehorned into `SessionRow`/`MEIC_CSV_COLUMNS`), consistent with Skyline being a standalone (not tranche/session-CSV) strategy per §4.

### 8.8 Dashboard

A minimal, additive dashboard surface — not a rewrite of the MEIC/Manual tabs (§3.4 defers full generic-loop refactor):

- New card/section: "Skyline (Paper)" showing today's `entered`/`skip_reason` (including the new `no_prior_close` and FOMC-day reasons), the 4 butterflies with per-leg fill status, `net_debit_total`, and running mark-to-market using existing option-mid fetch (`fetch_option_mids_api`).
- **Close All** and **Close [BF1..BF4]** buttons, wired to `common/manual_close.py` (§6.5) via the generic strategy-overrides-adjacent action endpoints, e.g. `POST /api/strategy/SKYLINE_SPX/close` (`{"structure": "BF1"}` or `{"structure": "all"}`) — a small, Skyline-agnostic addition to the same API surface introduced in §7.4, not a bespoke Skyline-only route.
- History view lists settled structures from `trades/history/SKYLINE_SPX/` with realized P/L, and flags any that were closed manually vs. held to settlement.
- Explicit **PAPER** badge (§3.4) on every element of this card — never mixed into live MEIC/Manual PnL totals.

---

## 9. Implementation plan

Each phase below corresponds to one Part from §0 and is independently mergeable/testable/revertable, per the guardrails in §1.7.

### Phase 1 — Per-strategy paper plumbing (Part A, §3) + FOMC/price-recording fix (Part C, §5)

Grouped together because both are small, foundational, and carry no interdependency on the rest of the document.

1. `strategies/loader.py`: read `paper`, `confirmed`, `exit_profile`/`stop_profile` alias; apply `config_overrides` onto constructed config.
2. `common/broker_factory.py`: add `get_shared_broker_for_strategy()`; generalize internal cache to a dict keyed by `(broker, paper, strategy_name, pid)`; keep `get_shared_broker()` behavior-identical for existing callers.
3. `common/trades_layout.py`: generalize `active_dir_for_strategy`/`history_dir_for_strategy` to any name; verify existing MEIC/Manual paths unchanged (regression test asserting byte-identical paths pre/post change).
4. `strategies/validate.py`: require `TASTYWARE_API_KEY` at startup for any enabled strategy with `paper: true`; enforce `confirmed: true` for any strategy with `requires_confirmation = True`.
5. `run.py`: add `is_market_closed_today()` and reorder `main()` per §5.1 so market_data/streamer start on FOMC/day-after-FOMC days (market open); a real NYSE holiday still exits `main()` immediately, unchanged.

### Phase 2 — Standalone strategy harness + N-leg broker primitive + Exit Conditions foundation (Parts B + D, §4 + §6)

1. `brokers/base.py` + `brokers/tastytrade_broker.py`: add `ComboLeg` + `place_multi_leg_order` (open and close, 4-leg ceiling enforced); extend `_order_result_from_placed_order` fallback path for N-leg fills; extend test `MockBroker`.
2. `blocks/stop/phases_profit_target.py`: `PhaseProfitTarget`; `blocks/stop/breach.py`: `spread_type`-aware debit support.
3. `common/portfolio_guard.py`: `PortfolioGuard`; wire halt-file check into `blocks/entry/runner.py` and the new `StandaloneStrategyRunner`.
4. `common/manual_close.py`: `close_structure`/`close_all_structures`.
5. `blocks/stop/profiles/hold_to_expiry.py`: reusable monitor-only profile factory; `blocks/stop/monitor.py`/`runner.py`: gate automated-close branch on `bool(profile.phases)`, keep mark/settlement refresh always on.
6. `common/standalone_runner.py`: new `StandaloneStrategyRunner` (broker resolution, daily idempotency file, crash-safe resume, `status()`, `apply_daily_overrides()`).
7. `run.py`: wire `StandaloneStrategyRunner` instances for enabled standalone strategies alongside existing MEIC tranche orchestration (additive).
8. `strategies/validate.py`: branch validation so standalone strategies skip `CreditEntryConfig` validation.

### Phase 2.5 — Daily dashboard overrides (Part E, §7)

1. `common/standalone_runner.py`: `apply_daily_overrides()` merge logic (extends the Phase 2 class — sequenced right after it since it depends on the state-file shape from §4.2).
2. `dashboard/server.py`: generic `GET/POST/DELETE /api/strategy/<name>/overrides` routes.
3. Validation reusing the existing unknown-key-rejection pattern from `CreditEntryConfig.from_overrides`.

### Phase 3 — Skyline strategy (Part F, §8)

1. `strategies/skyline/strategy.py` + `strategies/skyline/config.py`: `SkylineConfig` (`requires_confirmation = True`, `paper: bool = True` default), `SkylineStrategy(BaseStrategy)` — `schedule()` → `[]`, `run()` → trading-day + gap checks → structure build → 4× `place_multi_leg_order` → trade-JSON write, `stop()`/`get_status()`.
2. `strategies/skyline/legs.py`: pure function `build_butterfly_legs(atm, butterfly_cfg) -> List[ComboLeg]` — unit-testable without a broker.
3. `common/expiry_settlement.py`: extract `resolve_daily_close()` as a public function reusable by both existing settlement code and Skyline.
4. `config/strategies.yaml`: add `SKYLINE_SPX` entry (`enabled: false`, `confirmed: true` by default so it's ready to flip on with one field once approved).
5. `blocks/stop/profiles/skyline.py`: registers `skyline_hold_to_expiry` (uses the Phase 2 `hold_to_expiry_profile` factory).
6. Dashboard: read/write Skyline card + manual close buttons + history listing (§8.8), built on the Phase 2.5 generic API surface.
7. EOD settlement reconciliation for `trades/active/SKYLINE_SPX/*.json` → `trades/history/SKYLINE_SPX/`, using `resolve_daily_close()` for today's settlement price.

### Phase 4 — Docs / operator sign-off

1. Mark this spec `IMPLEMENTED` once Phases 1–3 are merged and Skyline has run at least one full paper session end-to-end (entry → settlement) without operator intervention, plus at least one manual-close exercised successfully in paper.
2. Record the first N paper-trading days' outcomes in a `changes/LIVE_SESSION_...` or dedicated `SKYLINE_PAPER_LOG.md` — this is explicitly for observing real fills/decay/settlement (§8.0), not for validating the backtest's edge.

**Status (2026-07-30):** Phases 1–3 complete on `feat/multi-strategy-framework`.
Phase 4 scheduled for next trading session — see §17.

---

## 10. Risks / known v1 limitations

| Risk | Mitigation / acceptance |
|---|---|
| Backtest edge is unverified/contested (§8.0) | Paper-only, no live path in this spec, by design |
| Partial-structure fills (some of 4 butterflies fill, others don't) | Recorded explicitly per-structure on the trade JSON; dashboard shows per-butterfly status; no auto-retry/chase in v1 (§8.5) |
| ET vs. CT clock mismatch risk (rest of codebase is CT-anchored) | Skyline's entry-window check explicitly converts to `America/New_York`; add a unit test pinning a DST-transition date to catch off-by-one-hour bugs |
| N-leg order fill-price attribution is approximate (order-limit fallback, §4.3) | Acceptable — v1 has no active management that depends on precise realized net credit, only fill/no-fill and settlement P/L computed from strikes vs. settlement price, not from fill-price bookkeeping |
| 4-leg-per-ticket platform ceiling means 4 separate commission/slippage tickets, always | Accepted — this is a broker constraint, not a design choice; documented in §1.3 so it's never mistaken for a future "just combine them" TODO |
| Manual close could be clicked accidentally | Dashboard confirms before submitting (standard UI pattern); logged distinctly (`MANUAL_CLOSE_*`) for audit; broker-side fill confirmation required before trade JSON marks `closed` |
| Portfolio Guard reads trade JSONs for P/L aggregation — could be slow with many open positions | Scoped to one strategy's files at a time (not account-wide); acceptable for MEIC/Skyline's expected position counts (single digits per day) |
| FOMC-day price-recording fix (§5) touches `run.py:main()` startup order | Narrow, single-function change; explicit regression tests assert zero behavior change on a normal trading day (C5) and on a real holiday (C1) — only the FOMC/day-after-FOMC rows change |
| New strategy could accidentally share `pause_tranches.json` / dashboard global kill-switch semantics with MEIC | Skyline's `StandaloneStrategyRunner` must still respect the account-wide kill-switch and new-risk gate (§4.3 note) — it is not exempt from global safety controls, only from MEIC-specific session-CSV/tranche pause semantics |
| Adding a 3rd strategy to dashboard hard-coded MEIC/Manual branches could destabilize existing PnL views | v1 dashboard change is additive-only (new card + generic override/close API, §7.4/§8.8) — zero changes to existing `STRATEGY_MEIC`/`STRATEGY_MANUAL` branches |
| Exit Conditions framework (Part D) could be seen as "starting down the V4 path again" | Explicitly scoped to only what Skyline/the operator's stated MEIC use case need (§1.7 point 5); no abstract `ExitPolicy`/`SignalGate`-style interfaces; every phase/guard is concrete and independently testable |

---

## 11. Non-blocking follow-ups (explicitly deferred)

- Full generic-loop dashboard refactor (strategy registry drives tabs/PnL keys instead of hardcoded MEIC/Manual branches) — §3.4.
- Leg-netting across Skyline's 4 butterflies — capped at 4 legs/ticket by the broker platform regardless (§1.3); only worth revisiting if strikes coincide across butterflies in a future config and only up to that ceiling.
- Discretionary adjustment support (valley-lifting condor, breakout-side extra butterfly) — §2 non-goals, §8.6.
- Cross-strategy (account-wide) portfolio risk aggregation, as opposed to the per-strategy Portfolio Guard in §6.4.
- Enabling the Portfolio Guard (§6.4) for MEIC by default — the capability ships; turning it on for the live strategy is a separate operator decision.
- Any live-trading promotion path for Skyline — requires a new design doc (§2 non-goals).
- Reviving `strategies/iron_fly/` on top of the same `StandaloneStrategyRunner` + Exit Conditions machinery (currently dormant/unregistered) — natural second consumer of Parts B/D once they exist, not required to ship Skyline.
- Migrating MEIC/Manual's session-CSV overrides onto the same file format as §7.3's standalone override file — not needed; the two patterns can coexist indefinitely.

---

## 12. File touch list (complete, all parts)

| File | Change |
|---|---|
| `strategies/loader.py` | Read `paper`/`confirmed`/`exit_profile` alias; apply `config_overrides` to constructed config |
| `common/broker_factory.py` | `get_shared_broker_for_strategy()`; multi-key shared broker cache |
| `common/trades_layout.py` | Generalize active/history dir helpers beyond 2 hardcoded names |
| `strategies/validate.py` | Require `TASTYWARE_API_KEY` for `paper: true`; require `confirmed: true` when `requires_confirmation`; standalone-strategy validation branch |
| `run.py` | Add `is_market_closed_today()`; reorder `main()` so a real holiday still exits immediately but FOMC/day-after-FOMC days no longer gate market_data/streamer (§5); wire `StandaloneStrategyRunner` |
| `brokers/base.py`, `brokers/tastytrade_broker.py` | `ComboLeg`, `place_multi_leg_order` (open + close, 4-leg ceiling); N-leg fill-result fallback |
| `blocks/stop/phases_profit_target.py` (new) | `PhaseProfitTarget` |
| `blocks/stop/breach.py` | `spread_type`-aware debit support |
| `common/portfolio_guard.py` (new) | `PortfolioGuardConfig`, `PortfolioGuard` |
| `common/manual_close.py` (new) | `close_structure`, `close_all_structures` |
| `blocks/stop/profiles/hold_to_expiry.py` (new) | Reusable monitor-only profile factory |
| `blocks/stop/monitor.py` / `blocks/stop/runner.py` | Gate automated-close on non-empty `profile.phases`; always refresh marks/settlement |
| `blocks/entry/runner.py` | Extra halt-file check (inert unless a Portfolio Guard is configured) |
| `common/standalone_runner.py` (new) | `StandaloneStrategyRunner` harness incl. `apply_daily_overrides()` |
| `dashboard/server.py` | Generic `GET/POST/DELETE /api/strategy/<name>/overrides`; `POST /api/strategy/<name>/close`; additive read-only Skyline card |
| `common/expiry_settlement.py` | Extract public `resolve_daily_close()`, reused by settlement + Skyline gap check |
| `strategies/skyline/strategy.py`, `config.py`, `legs.py` (new) | Skyline strategy implementation |
| `config/strategies.yaml` | `SKYLINE_SPX` entry (`enabled: false`, `confirmed: true`) |
| `blocks/stop/profiles/skyline.py` (new) | Registers `skyline_hold_to_expiry` |
| `tests/test_broker_factory_multi_strategy.py` (new) | Per-strategy shared broker isolation |
| `tests/test_standalone_runner.py` (new) | Idempotent daily entry, crash-safe resume, override merge |
| `tests/test_portfolio_guard.py` (new) | P/L aggregation, halt/close behavior |
| `tests/test_manual_close.py` (new) | Reversed-leg construction, all/one close |
| `tests/test_skyline_legs.py` (new) | `build_butterfly_legs` strike math, pure/no-broker |
| `tests/test_skyline_strategy.py` (new) | Gap-down skip, no-prior-close skip, FOMC skip, partial fill handling, no-stop attach |
| `tests/test_run_one_day.py` | FOMC/day-after-FOMC day still starts market_data recorder; real NYSE holiday still exits immediately (unchanged) |
| `tests/test_strategy_loader.py`, `tests/test_strategy_validation.py` | Extend for `paper`/`confirmed`/`exit_profile` + `SKYLINE_SPX` entry |

---

## 13. Required tests (complete, all parts)

See also the per-part test tables in §5.4, §6.9, and §7.6, reproduced/summarized here for the full regression command.

| # | Test |
|---|---|
| T1 | `paper: true` in yaml sets `StrategyConfig.paper` (loader applies it) |
| T2 | `config_overrides` are actually applied to the constructed strategy config (closes existing gap) |
| T3 | `get_shared_broker_for_strategy('MEIC_IC', paper=False)` and `get_shared_broker_for_strategy('SKYLINE_SPX', paper=True)` return distinct, independently-cached broker instances in the same process |
| T4 | Startup validation raises if an enabled strategy has `paper: true` and `TASTYWARE_API_KEY` is unset |
| T5 | Runtime order-path assertion halts the strategy (no order sent) if `config.paper` is `True` but the resolved broker's `_is_paper` is `False` |
| T6 | `place_multi_leg_order` builds a single `NewOrder` with N legs in the correct action/quantity order (mock SDK); raises `ValueError` for >4 legs |
| T7 | `place_multi_leg_order` with reversed actions correctly closes a previously-opened combo (used by manual close) |
| C1–C6 | FOMC-day price-recording fix — see §5.4 |
| D1–D10 | Exit Conditions framework — see §6.9 |
| E1–E6 | Daily dashboard overrides — see §7.6 |
| T8 | `build_butterfly_legs` produces the exact 16 legs / 4 strikes-per-fly from §8.2's default table given a fixed ATM |
| T9 | Gap-down skip: spot 26 points below prior close ⇒ no orders, `skip_reason='gap_down'`; 24 points below ⇒ proceeds; any rally magnitude ⇒ proceeds (asymmetric filter) |
| T10 | `resolve_daily_close()` correctly falls back through `ohlc_daily` → `spx_polls` → `spx_1m`; returns `None` (triggering `no_prior_close` skip) when none exist — no external API call is ever made |
| T11 | Partial fill: 3 of 4 butterflies fill within `entry_fill_wait_sec`, 1 cancelled ⇒ trade JSON reflects per-structure status, no auto-retry |
| T12 | `SKYLINE_SPX` requires both `enabled: true` and `confirmed: true` in the **same yaml entry** to load; either alone is insufficient; no `.env` flag is read for this |
| T13 | Existing `tests/test_strategy_loader.py` / `test_strategy_validation.py` MEIC/Manual assertions stay green (no regression from generalization) |

### Regression

```bash
pytest tests/test_strategy_loader.py tests/test_strategy_validation.py \
       tests/test_broker_factory_multi_strategy.py tests/test_standalone_runner.py \
       tests/test_portfolio_guard.py tests/test_manual_close.py \
       tests/test_skyline_legs.py tests/test_skyline_strategy.py \
       tests/test_run_one_day.py \
       tests/test_stop_runner_gate.py tests/test_stop_monitor_engine.py -q
```

---

## 14. Acceptance criteria (complete, all parts)

| # | Criterion |
|---|---|
| A1 | A strategy's `paper` flag in `config/strategies.yaml` is honored independent of global `PAPER_MODE` |
| A2 | MEIC_IC (live) and SKYLINE_SPX (paper) run concurrently in one launcher process without broker/session collision |
| A3 | Skyline cannot place a live order under any configuration error — two independent fail-closed checks (§3.3) |
| A4 | Skyline is disabled by default; requires both `enabled: true` and `confirmed: true` **in `config/strategies.yaml` only** — no `.env` involvement |
| A5 | Skyline entry reproduces the source's 4-butterfly strike table exactly (defaults), fully configurable without code changes |
| A6 | Skyline has zero *automated* exit/stop management — holds to expiration by default — but manual "close all" / "close one" is available and works |
| A7 | An FOMC or day-after-FOMC day still records SPX prices via `market_data` (market is open); only strategy trading is skipped. A real NYSE holiday is unaffected — everything stays off exactly as today, since the market never opens |
| A8 | A strategy can be paused/adjusted for **today only** from the dashboard without editing `config/strategies.yaml` |
| A9 | A strategy can opt into a whole-strategy daily loss cap that halts new entries and (optionally) closes open positions |
| A10 | Existing MEIC/Manual entry, stop, session-CSV, and dashboard PnL code paths are unchanged (zero regressions) |
| A11 | New N-leg order primitive is generic (not Skyline-specific), enforces the 4-leg ceiling, and supports both opening and closing |
| A12 | Trade/dashboard data clearly and by-default excludes paper trades from live PnL aggregates |
| A13 | Skyline's gap-down check uses only data the bot already records (`data/<date>/SPX_*.csv` via `resolve_daily_close()`) — no external API call |
| A14 | All tests in §13 (and per-part §5.4/§6.9/§7.6) pass; full existing suite stays green |

---

## 15. Decision log

| Date | Decision |
|---|---|
| 2026-07-28 | Initial draft. Chose incremental extension of existing `strategies/*` registry over the V4 rewrite (§1.7) — scoped to what's needed for per-strategy paper trading + one new defined-risk strategy. Skyline strikes taken verbatim from source article defaults, fully config-driven. No live path for Skyline in this document by design, given contested backtest reproduction reported in the source's own comments. |
| 2026-07-30 | Split into Overview (plain English) + this Technical companion. |
| 2026-07-30 | Incorporated operator feedback: (1) added Part D, Exit Conditions framework — profit-target phase, debit-side support, per-strategy Portfolio Guard loss cap, generic manual close primitive — explicitly scoped and additive to avoid repeating the V4 rewrite failure (§1.7); (2) documented the 4-leg-per-order platform ceiling as the real reason butterflies aren't netted into fewer tickets (§1.3), not just a v1 simplicity choice; (3) added Part E, generalizing MEIC's existing session-CSV daily-override pattern into a strategy-agnostic dashboard override mechanism (§7); (4) sourced Skyline's gap-down prior-close from the bot's own recorded SPX CSVs via a new public `resolve_daily_close()` helper, explicitly rejecting an external data vendor (§8.3); (5) identified and scoped a fix for an existing bug where FOMC/holiday days stop SPX price recording entirely, not just trading (Part C, §5); (6) replaced the `.env SKYLINE_ENABLED` idea with a single-file `enabled`+`confirmed` pair in `config/strategies.yaml` (§8.4), keeping all strategy-level configuration in one place. |
| 2026-07-30 | Corrected the Part C fix per operator pushback: "on holidays there is no point of running anything, SPX prices won't be changing." The earlier draft would have kept `run.py` (streamer/market_data/stop_monitor) running on *any* day `check_trading_day()` flags, including real NYSE holidays — that's wrong, since the exchange is closed and there's nothing to record. Split the fix into `is_market_closed_today()` (holiday calendar only, gates everything, unchanged from today) vs. `check_trading_day()` (unchanged, still gates strategy trading only) so the fix now applies only to FOMC/day-after-FOMC days, when the market is genuinely open (§1.5, §5). |
| 2026-07-30 | **Implementation landed on `feat/multi-strategy-framework`.** Phases 1–3 + History tab (§16). Skyline live path wired but default `paper: true`. Phase 4 (paper-day sign-off) pending. |

---

## 16. Implementation status — file by file (2026-07-30)

Legend: **Done** | **Partial** | **Pending**

### Phase 1 — Per-strategy paper + FOMC fix

| File | Status | Notes |
|---|---|---|
| `strategies/loader.py` | Done | `paper`, `confirmed`, `exit_profile` alias, `config_overrides` applied |
| `strategies/base.py` | Done | `confirmed`, `requires_confirmation`, `apply_config_overrides()` |
| `common/broker_factory.py` | Done | `get_shared_broker_for_strategy()` |
| `common/trades_layout.py` | Done | Generalized paths; `STRATEGY_SKYLINE`, `registered_strategies()` |
| `strategies/validate.py` | Done | Paper API key, `confirmed` gate, live creds when `paper: false` |
| `run.py` | Done | `is_market_closed_today()`, FOMC split, standalone runners |
| `tests/test_run_one_day.py` | Done | Holiday vs FOMC split tests |
| `tests/test_broker_factory_multi_strategy.py` | Done | |
| `tests/test_trades_layout.py` | Done | |

### Phase 2 — Standalone + N-leg + Exit Conditions

| File | Status | Notes |
|---|---|---|
| `brokers/base.py`, `tastytrade_broker.py` | Done | `ComboLeg`, `place_multi_leg_order` |
| `brokers/__init__.py` | Done | Lazy import fix |
| `tests/mock_broker.py` | Done | Multi-leg stub |
| `common/standalone_runner.py` | Done | Broker, daily state, overrides, portfolio guard |
| `blocks/stop/profiles/hold_to_expiry.py` | Done | Includes `skyline_hold_to_expiry` registration |
| `blocks/stop/phases_profit_target.py` | Partial | Class exists; **not registered** in any profile |
| `blocks/stop/breach.py` | Done | Debit/credit + profit-target math |
| `common/portfolio_guard.py` | Done | Opt-in via `max_daily_loss` |
| `common/manual_close.py` | Done | Config-driven `paper` flag |
| `blocks/entry/runner.py` | Done | Portfolio guard halt check |
| `tests/test_breach_math.py` | Done | |
| `tests/test_portfolio_guard.py` | Done | |
| `tests/test_manual_close.py` | Done | |
| `blocks/stop/monitor.py` / `runner.py` | Partial | Empty `phases=[]` is no-op; no explicit gate + no Skyline JSON MTM |

### Phase 2.5 — Daily overrides

| File | Status | Notes |
|---|---|---|
| `common/daily_overrides.py` | Done | |
| `dashboard/server.py` | Done | `GET/POST/DELETE /api/strategy/<name>/overrides`, `/close` |
| Dashboard overrides UI (full) | Pending | Pause Today only on Skyline tab |

### Phase 3 — Skyline

| File | Status | Notes |
|---|---|---|
| `strategies/skyline/strategy.py` | Done | Gap-down, 4× combo, trade JSON; live path when `paper: false` |
| `strategies/skyline/legs.py` | Done | |
| `strategies/skyline/settlement.py` | Done | MTM + EOD settlement |
| `common/expiry_settlement.py` | Done | `resolve_daily_close()`, Skyline path scanning |
| `common/history_adapter.py` | Done | Skyline → SQLite history row |
| `config/strategies.yaml` | Done | `SKYLINE_SPX` entry (`enabled: false`, `confirmed: false`) |
| `common/session_cleanup.py` | Done | Skyline EOD settlement hook |
| `dashboard/templates/index.html` | Done | Skyline tab, History money filter, paper badge |
| `dashboard/db.py` | Done | `paper` column; live/paper stats filters |
| `.env.example` | Done | TASTYWARE note for paper strategies |
| `tests/test_skyline_legs.py` | Done | |
| `tests/test_skyline_settlement.py` | Done | |
| `tests/test_history_adapter.py` | Done | |
| `tests/test_daily_overrides.py` | Done | |
| `tests/test_standalone_runner.py` | Pending | Not written |
| `tests/test_skyline_strategy.py` | Pending | Integration tests (gap-down, partial fill) |
| `tests/test_strategy_loader.py` | Pending | Skyline `confirmed`/`paper` cases |

### Phase 4 — Sign-off

| Item | Status |
|---|---|
| Full paper day (entry → settlement) | **Pending tomorrow** |
| Manual close exercised in paper | **Pending tomorrow** |
| `changes/SKYLINE_PAPER_LOG.md` | Pending |
| Spec status → `IMPLEMENTED` | Pending |
| Merge to `master` | Pending operator decision |

---

## 17. Tomorrow's paper test + post-test backlog

### 17.1 Pre-flight (before market open)

```yaml
# config/strategies.yaml
  - name: SKYLINE_SPX
    enabled: true
    confirmed: true
    paper: true
```

```env
# .env
PAPER_MODE=false
TASTYWARE_API_KEY=<set>
BROKER=tastytrade
```

```bash
git checkout feat/multi-strategy-framework
python run.py
# Dashboard: http://localhost:5002
```

Confirm logs show: MEIC live broker + Skyline paper broker + standalone runner started.

### 17.2 During session — observe

| Time (ET) | What to check |
|---|---|
| Pre-9:45 | Skyline tab: Waiting; Pause Today works |
| 9:45 | Entry attempt; gap-down skip if applicable |
| Post-entry | 4 butterflies; per-structure fill status |
| Mid-day | Unrealized P/L on Skyline tab; MEIC unaffected |
| Optional | Manual close one butterfly; then Close All on remainder |
| ~3:30 PM CT | EOD cleanup settles to `trades/history/SKYLINE_SPX/` |
| After close | History tab → Paper $ → Skyline row with realized P/L |

### 17.3 Log template (`changes/SKYLINE_PAPER_LOG.md`)

Create after the session with:

- Date, branch, config snapshot
- Entry: ATM, spot, prior close, gap-down Y/N
- Per-butterfly: filled / cancelled / net debit
- Mid-day MTM snapshot (optional)
- Manual close exercised? Y/N
- EOD settlement: spot, realized P/L per structure
- Issues / surprises

### 17.4 Post-test backlog (pick up after paper day)

| Priority | Item | Notes |
|---|---|---|
| P0 | Paper-day log + mark spec `IMPLEMENTED` | Unblocks merge |
| P1 | Verify settlement P/L × 100 multiplier vs broker | `settlement.py` / `history_adapter.py` |
| P1 | Merge `feat/multi-strategy-framework` → `master` | If paper day clean |
| P2 | Dashboard overrides UI (beyond Pause Today) | API exists |
| P2 | `test_strategy_loader.py` / `test_strategy_validation.py` for Skyline | |
| P2 | `test_skyline_strategy.py` integration tests | Gap-down, partial fill |
| P3 | Register `PhaseProfitTarget` in a profile | MEIC opt-in only |
| P3 | Attach Skyline to `stop_monitor` monitor-only profile | Optional unified MTM |
| P3 | `test_standalone_runner.py` | Idempotency, crash resume |
| P3 | Full pytest suite in project venv | `paho-mqtt`, `pandas_market_calendars` |
| P4 | Portfolio guard on MEIC | `config_overrides.max_daily_loss` |
| P4 | Generic dashboard loop refactor | Deferred non-goal |
| — | Live Skyline (`paper: false`) | **Not planned** — requires separate sign-off |

### 17.5 Rollback

```bash
git checkout master   # drops all framework work
```

Or stay on branch and set `enabled: false` on Skyline to disable without reverting code.
