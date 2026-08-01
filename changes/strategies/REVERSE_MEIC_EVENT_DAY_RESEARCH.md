# "Reverse MEIC" for FOMC / High-Impact News Days — Research Note

**Status:** RESEARCH ONLY — no design spec, no implementation, no live path
**Date:** 2026-07-31
**Related:** [STRATEGY_DESIGN_PATTERN.md](../design/STRATEGY_DESIGN_PATTERN.md) (this would be a **standalone** strategy per that doc if pursued), [PAPER_TRADING_MULTI_STRATEGY_FRAMEWORK.md](PAPER_TRADING_MULTI_STRATEGY_FRAMEWORK.md) (N-leg combo primitive this would reuse)

## 0. Why this exists

MEIC is intentionally skipped on FOMC / CPI / NFP days — the mean-reversion, theta-decay edge it relies on doesn't hold when a scheduled catalyst is pending (the whole point of MEIC's day-of-week/event blackout). The question raised: **since we already sit out these days, is there a defined-risk, opposite-shaped trade that specifically profits from the catalyst-driven move, sized and exited well enough to be worth running?** This note is the research/evaluation pass on that question — not a decision to build it.

## 1. What "reverse MEIC" would mean

MEIC sells iron condors (credit, short vol, wants price to stay in range, wins from theta decay). The literature has a well-known mirror image:

- **Long / "reverse" / "debit" iron condor**: buy an inner call spread + inner put spread, sell the outer wings to partially offset the cost. Net **debit**. Profits if SPX moves beyond either inner breakeven by expiration; loses (up to the debit paid, and only the debit) if SPX pins near the center.
- **Long straddle** (buy ATM call + ATM put) or **long strangle** (buy OTM call + OTM put): simpler, no short legs, higher debit, uncapped-ish upside on a big move, but the entire debit decays to zero if the move doesn't happen.

Both are **defined-risk** (max loss = debit paid) — consistent with how this codebase treats every strategy (MEIC's stop-managed risk, Skyline's structurally-capped risk). Neither is naked/undefined-risk, so neither conflicts with the account's risk posture.

## 2. What the research actually supports

Sources reviewed: TradeAlgo, FlyOnTheWall, OptionsDecay, AdvancedAutoTrades, SpotGamma, 0DTE.solutions, FlashAlpha, and the `MEICAgent` project's own documented FOMC-blackout rationale. Search performed 2026-07-31; see §7 for links.

**Consensus points:**

1. A **debit/reverse iron condor** (not a naked straddle) is the standard "I expect a move but not a direction" defined-risk structure for scheduled catalysts (FOMC, CPI, NFP, earnings) — repeatedly recommended over a straddle specifically *because* the short outer wings cut the cost of the long vega/gamma exposure, which matters a lot when much of that exposure will evaporate the moment the number prints (IV crush).
2. **0DTE is explicitly called out as a bad DTE for a straddle/strangle bought for a multi-day thesis** ("premium is almost entirely intrinsic... requires an extreme intraday move") — but that critique is aimed at buying a straddle *at the open* and holding all day speculatively. It does not directly apply to a structure bought **shortly before a specific intraday scheduled catalyst** and exited **shortly after** — which is a different, narrower trade (a "catalyst window" trade, not an all-day directional bet). Multiple sources explicitly describe *that* narrower version favorably (FlashAlpha, AdvancedAutoTrades: "SPX weekly options before inflation reports... payoff can be quick... within 1-2 trading days"; note their examples are actually weekly/short-dated, not always literally 0DTE, so this is directionally supportive, not a same-day-specific backtest).
3. **Exit discipline is the entire edge**, and it's the same shape everywhere in the research:
   - Enter close to the event (minutes-to-an-hour before), not hours ahead — minimizes exposure to pre-event vega decay/drift that isn't the catalyst itself.
   - **Exit within roughly 15–30 minutes of the release**, independent of P&L, because IV crush hits immediately once the number is known even if price is still moving, and because theta/pin risk resume dominating once the "reason to move" is used up.
   - Profit-taking is intentionally *not* greedy: close winners early (25–50% of debit for straddles; more room for a capped debit-condor since max profit is bounded, so more like 50–75% of the structure's max profit is reasonable) rather than waiting for the theoretical max.
   - A time-based exit matters more here than a price-based stop-loss — several sources frame the mechanical stop as secondary to "the catalyst has resolved, get out."
4. MEIC's own literature (the `MEICAgent` doc reviewed) treats FOMC/CPI/NFP as **hard blackout days for credit condors**, full stop, no exception — reinforcing that these are genuinely different regimes, not just "MEIC but wider."

**Caveats / things the research does not give us:**

- No source provided a rigorously backtested SPX 0DTE-specific PT/SL rule set for this exact structure — the numbers above are triangulated from adjacent strategies (straddle exit discipline, general debit-condor mechanics), not a validated 0DTE reverse-condor backtest. This is directly analogous to Skyline's own credibility caveat (§6.0 of the framework doc) — **treat this the same way: paper-only, observation-first, no backtest claim taken at face value.**
- None of the sources address the specific mechanics of SPX 0DTE bid/ask spreads and fill quality for a 4-leg debit combo entered in a ~30-minute pre-event window, which is exactly the kind of thing that can quietly erase a structurally-sound edge (Skyline's own paper run already surfaced a real-world "leg didn't fill" risk for a similar 4-leg combo — see `SKYLINE_PAPER_LOG.md`).

## 3. Recommended structure (for evaluation, not yet a spec)

If this were to move to a design spec, the shape that best fits both the research and the existing codebase:

**Debit (reverse) iron condor, sized off the market's own expected move, entered in a short pre-event window, exited on a hard post-event time-box — not held to expiration.**

- Reuses `ComboLeg`/`place_multi_leg_order` exactly as Skyline's butterflies do (4 legs: buy inner call, sell outer call, buy inner put, sell outer put) — no new broker primitive needed.
- Standalone shape per `STRATEGY_DESIGN_PATTERN.md` §4 — single daily (or per-event) entry, not a tranche strategy.
- **Not** `hold_to_expiry` — this needs an *active*, time-based exit, which the strategy's own `run()` loop can own directly (a single `if now >= entry_time + post_event_window: close_all_legs()` check) without needing `stop_monitor` phase machinery at all. This is a good first real-world case for "standalone + simple owned exit" from the design-pattern doc (§4, bullet 2).

### Illustrative parameters (need real backtesting/observation before treating as anything but a starting point)

| Parameter | Illustrative default | Rationale |
|---|---|---|
| Eligible days | FOMC statement days only, to start (fewer, higher-conviction events than adding CPI/NFP immediately) | Smallest, cleanest event set; expand later if it holds up |
| Entry window | T-30min to T-10min before the scheduled release | Late enough to minimize pre-event decay/drift exposure, early enough to get filled without chasing |
| Inner long strikes | ATM ± ~45% of that day's SPX expected move (from options-implied straddle price) | Puts the long legs near where a "real" move should push price ITM |
| Outer short strikes | ATM ± ~90–100% of expected move | Caps both the debit paid and the max profit; keeps the structure defined-risk on both sides |
| Quantity | 1 contract to start, independent of MEIC/Skyline sizing | Unvalidated strategy — smallest reasonable size |
| Profit target | Close at 60–75% of max profit if reached | Capped structure allows being greedier than a straddle's 25–50% guidance |
| Stop-loss | Optional secondary check: close if debit position loses ~50% of its value *before* the release (signals something moved/leaked pre-announcement) | Secondary to the time-box, not the primary exit |
| Time-boxed exit | Force-close all legs 15–30 minutes after the scheduled release, regardless of P&L | The single most-repeated rule across every source reviewed — the catalyst has resolved, remaining time value only bleeds from here |
| Max hold | Never held to expiration/settlement — unlike Skyline, this structure is meant to be flat well before the close | Distinguishes this from Skyline's hold-to-expiry philosophy; needs its own exit style per §4 of the design-pattern doc |

## 4. What would be needed before this is even worth a paper test

1. **Economic calendar source** — FOMC (and later CPI/NFP) dates + exact release times. Small, infrequent list (8 FOMC/year); could be a static YAML list maintained by hand rather than a live feed integration, at least for v1.
2. **Expected-move input** — needs *some* number to size inner/outer strikes off of (ATM straddle price at entry time is the simplest; VIX-derived daily expected move is a fallback). Nothing in the codebase currently computes this — would be a new small helper, not a new data source (can be derived from the option chain already being fetched for entry).
3. A decision on whether CPI/NFP get added alongside FOMC, given they're more frequent but arguably lower-conviction moves than an FOMC decision.

## 5. Non-goals (if this is ever pursued)

- No live path without its own explicit design doc and sign-off, exactly like Skyline (§2 non-goals of the framework doc).
- No netting/combining with MEIC/Skyline sizing or risk budgets in v1 — independent, small, isolated.
- No CPI/NFP/earnings expansion until FOMC-only has a real paper track record.
- No price-based stop as the *primary* exit — the research is consistent that the time-box matters more than a P&L stop for this shape.

## 6. Suggested next step

Not a build yet. If this is worth pursuing: write a short design spec (following [STRATEGY_DESIGN_PATTERN.md](../design/STRATEGY_DESIGN_PATTERN.md)'s checklist) sized to a single FOMC day, paper-only, and get at least one real FOMC session of *live paper data* (journal-paper, same as Skyline) before writing any PT/SL numbers in stone. The parameters in §3 are a reasonable starting point for that first paper run, not a backtested edge.

## 7. References

- [How I Use Reverse Iron Condors to Beat the Market](https://advancedautotrades.com/reverse-iron-condor/)
- [Reverse Iron Condor: How to Profit from Breakout Moves with Defined Risk — TradeAlgo](https://www.tradealgo.com/trading-guides/options-strategies/reverse-iron-condor-how-to-profit-from-breakout-moves-with-defined-risk)
- [Iron Condor Options Strategy: The $4-to-$1 Trap — FlyOnTheWall](https://flyonthewall.ai/iron-condor-options-strategy/)
- [Iron Condor — OptionsDecay.com](https://www.optionsdecay.com/strategies/advanced/iron-condor/)
- [Trading the FOMC Meeting: 0DTE & Next Day Strategies — Option Alpha](https://optionalpha.com/blog/trading-the-fomc-meeting-0dte-next-day-strategies)
- [Debit Iron Condor — SpotGamma Support Center](https://support.spotgamma.com/hc/en-us/articles/16877137594643-Debit-Iron-Condor)
- [Long Straddle: Profit from Big Moves — OptionsDecay.com](https://www.optionsdecay.com/strategies/advanced/long-straddle/)
- [How to Trade a Long Straddle 0DTE — FlashAlpha](https://flashalpha.com/articles/long-straddle-0dte-flashalpha-live-entry-signal)
- [0DTE Iron Condor Strategy: Same-Day Income Guide — TradeAlgo](https://www.tradealgo.com/trading-guides/options/0dte-iron-condor)
- [0DTE Iron Condor Strategy: The Complete Setup — SweepAlgo](https://sweepalgo.com/0dte-iron-condor/)
- [joncovington/MEICAgent — README](https://github.com/joncovington/MEICAgent/blob/main/README.md) (documents the FOMC/CPI/NFP hard-blackout rationale this note starts from)
- [Tammy Chambless explains her MEIC strategy — Theta Profits](https://www.thetaprofits.com/tammy-chambless-explains-her-meic-strategy-for-trading-0dte-options/)
