# 0DTE Mean Reversion Strategy — Overview and Specification

**Status:** IMPLEMENTED (Paper Default)  
**Strategy Name:** `MEAN_REVERSION_SPX`  
**Reference:** [ThetaProfits — 0DTE Mean Reversion: Trading Against Big SPX Moves with Credit Spreads](https://www.thetaprofits.com/0dte-mean-reversion-trading-against-big-spx-moves-with-credit-spreads/) (Jamaal Ghauri)

---

## 1. Executive Summary

This strategy implements a systematic 0DTE mean reversion approach for SPX. Rather than chasing momentum, the strategy waits for the market to move to the outer boundaries of its options-market implied range (the **Expected Move**) and fades the extension using directional vertical credit spreads.

The strategy is built as a non-tranche standalone strategy adhering directly to `STRATEGY_DESIGN_PATTERN.md`, running via `StandaloneStrategyRunner` with paper trading enabled by default.

---

## 2. Core Mechanics

### 2.1 Expected Move Calculation (8:30 AM CST / 9:30 AM ET)
- At market open (8:30 AM CST), the strategy identifies the At-The-Money (ATM) strike:
  $$\text{ATM Strike} = \text{round}\left(\frac{\text{SPX Spot}}{5}\right) \times 5$$
- The 0DTE ATM Call and Put mids are fetched to determine the ATM Straddle Price:
  $$\text{Straddle Price} = \text{Call}_{\text{ATM Mid}} + \text{Put}_{\text{ATM Mid}}$$
- The Expected Move ($EM$) is established:
  $$EM = \text{Straddle Price} \times \text{Multiplier} \quad (\text{default multiplier} = 1.0)$$
- Outer trigger boundaries are locked for the session:
  $$\text{Upper EM} = \text{Open SPX} + EM$$
  $$\text{Lower EM} = \text{Open SPX} - EM$$

### 2.2 Entry Triggers
- **Upper EM Breach**: If $\text{SPX Spot} \ge \text{Upper EM}$ before the session cutoff (12:00 PM CST / 1:00 PM ET):
  - **Signal:** `UPPER_EM_BREACH`
  - **Action:** Enter a **Bear Call Spread** (fade upside move, anticipating mean reversion downward).
- **Lower EM Breach**: If $\text{SPX Spot} \le \text{Lower EM}$ before the session cutoff:
  - **Signal:** `LOWER_EM_BREACH`
  - **Action:** Enter a **Bull Put Spread** (fade downside move, anticipating bounce upward).
- **No Confirmation Delay**: As detailed in Jamaal Ghauri's strategy rules, the system enters immediately upon touching the level without waiting for candle closes, preventing slippage on rapid reversals.
- **Session Cutoff**: No new entries are taken after 12:00 PM CST (1:00 PM ET), focusing on early-session mean-reverting flows.

### 2.3 Strike Selection & Structure
- **Spread Width**: Default 10 points wide (configurable between 5 and 20 points).
- **Short Strike Delta**: Preferred range between $10\Delta$ and $25\Delta$ (default target: $18\Delta$).
  - For Call Spread: Short Call strike with delta $\approx 0.18$ ($0.10 \le \Delta \le 0.25$), Long Call strike = Short Strike + 10.
  - For Put Spread: Short Put strike with delta $\approx -0.18$ ($-0.25 \le \Delta \le -0.10$), Long Put strike = Short Strike - 10.
- **Delta Engine**: Real-time TastyTrade DXLink streamer Greeks with an analytical Black-Scholes 0DTE delta fallback.

---

## 3. Exit Mechanics & Risk Management

1. **Profit Target (50%)**:
   - Limit exit triggered when spread mark price decays to $50\%$ of credit received:
     $$\text{Target Mark} = \text{Entry Credit} \times (1.0 - 0.50)$$
2. **Initial Stop Loss (100% of Credit)**:
   - Initial maximum loss is capped at $1.0\times$ credit received (spread mark reaches $2.0\times$ entry credit):
     $$\text{Initial Stop} = \text{Entry Credit} \times 2.0$$
3. **Breakeven / Lock-in Stop (+25% Gain Trigger)**:
   - Once the spread achieves $25\%$ unrealized profit, the active stop automatically moves to breakeven with a 10% gain lock:
     $$\text{Breakeven Stop} = \text{Entry Credit} \times (1.0 - 0.10)$$
   - Prevents winning mean-reversion trades from turning into losses.
4. **End of Day Cutoff (14:55 CST)**:
   - Any open position is closed before market close to eliminate overnight or pin risk.

---

## 4. Configuration (`config/strategies.yaml`)

```yaml
  - name: MEAN_REVERSION_SPX
    enabled: true
    confirmed: true
    module: strategies.mean_reversion.strategy
    instrument: SPX
    broker: tastytrade
    stop_profile: mean_reversion
    scheduled: false
    paper: true
    config_overrides:
      open_time_cst: '08:30'
      cutoff_time_cst: '12:00'
      market_close_cst: '14:55'
      spread_width: 10
      target_delta: 0.18
      min_delta: 0.10
      max_delta: 0.25
      quantity: 1
      profit_target_pct: 0.50
      stop_loss_pct: 1.00
      breakeven_trigger_pct: 0.25
      breakeven_lock_pct: 0.10
```

---

## 5. Architectural Integrity & Checklist Adherence

- **Additive Implementation**: Zero lines changed in MEIC or Manual live execution paths.
- **Safety First**: Paper trading (`paper: true`) is enforced by default with broker safety assertion.
- **Unified Trades Layout**: Full active/history path support under `trades/active/MEAN_REVERSION_SPX/` and `trades/history/MEAN_REVERSION_SPX/`.
- **Streamer Registration**: Real-time option symbol registration for mark-to-market and logging.
- **Dashboard UI**: Dedicated tab with high-contrast dark theme, live EM levels, trigger badges, position mark, and historical paper trades table.
- **Test Coverage**: Pure unit tests for leg builders, analytical delta calculations, expected move boundaries, risk thresholds, and runner lifecycle in `tests/test_mean_reversion.py`.
