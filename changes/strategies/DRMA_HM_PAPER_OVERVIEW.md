# DRMA + Hilega-Milega Pro (DRMA_HM) 0DTE SPX Strategy

**Status:** SHIPPED (PAPER)  
**Date:** October 11, 2026  
**Implementation Location:** [`strategies/drma_hm/`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/strategies/drma_hm/)  
**Based on:** [`Daily_Reset_MA_Hilega_Milega_Pro.pine`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/Daily_Reset_MA_Hilega_Milega_Pro.pine)  

---

## 1. Overview & Objectives

The **DRMA + Hilega-Milega Pro** strategy translates the institutional TradingView Pine Script strategy into a standalone, paper-first automated trading system in the MEIC project. It leverages daily SPX 5-minute bars collected by the MEIC market recorder (`data/YYYY-MM-DD/SPX_5m.csv`), overnight Globex continuous ES futures (`ES=F`) with basis offset, and multi-timeframe Hilega-Milega momentum.

### Key Capabilities
1. **Directional Expansion Trades**:
   - Fires on **Grade A+ (5/5)** confluence.
   - Executes Bull Call Debit Spreads (CDS) or Bear Put Debit Spreads (PDS).
   - Configurable target selection by **Delta** (e.g. 0.40 delta) or **Debit** (e.g. $2.50 debit).
2. **Non-Directional / Credit Harvest Trades**:
   - Fires on **Grade A (4/5)** confluence or neutral chop within the 30m ORB.
   - Executes Bull Put Credit Spreads (PCS) when bullish, Bear Call Credit Spreads (CCS) when bearish, or neutral Iron Condors.
   - Configurable target selection by **Delta** (e.g. 0.15 delta) or **Credit** (e.g. $1.00 credit).
3. **Automated ES & SPX Overnight Levels via Yahoo Finance with Manual Overrides**:
   - Queries `https://query1.finance.yahoo.com/v8/finance/chart/ES=F` for continuous Globex overnight bars (16:00 ET to 09:30 ET).
   - Computes Overnight High (ONH), Overnight Low (ONL), Previous RTH Close (PDC), and the ES-to-SPX cash basis offset (`scaled_es_close - spx_close`).
   - If Yahoo Finance is unavailable or offline, seamlessly falls back to reading the previous trading day's local file (`data/<prev_date>/SPX_5m.csv`).
   - Supports explicit manual overrides (`manual_onh`, `manual_onl`, `manual_pdh`, `manual_pdl`, `manual_pdc`, `manual_es_offset`).
4. **Position Management & Exits**:
   - Profit Target (PT) and Stop Loss (SL) based on percentage of entry credit/debit.
   - Dynamic Trend Runner 21 EMA trailing stop (2 consecutive bar close across 21 EMA or full H-M flip across 50 line).
   - Mandatory EOD Force-Flat at 14:50 CST (15:50 ET) ensuring zero overnight risk.

---

## 2. Confluence Scoring Matrix (0–5)

Each 5-minute bar evaluates the institutional setup rules from the Pine Script:

| Confluence Dimension | Bullish Rule | Bearish Rule |
| :--- | :--- | :--- |
| **1. Key Level Break** | `close > ORB_High` OR `close > PDH` OR `close > ONH` OR (cross above 21 EMA and `close > ORB_Mid`) | `close < ORB_Low` OR `close < PDL` OR `close < ONL` OR (cross below 21 EMA and `close < ORB_Mid`) |
| **2. Trend Alignment** | `close > 21 EMA` | `close < 21 EMA` |
| **3. Active MA Slope** | `MA - MA[2] >= min_slope_pts` (0.50 pts) | `MA[2] - MA >= min_slope_pts` (0.50 pts) |
| **4. H-M Conviction** | `Fast > Slow > 50` (Super Bullish) | `Fast < Slow < 50` (Super Bearish) |
| **5. Extension Cap** | `abs(close - 21 EMA) <= 18.0 pts` | `abs(close - 21 EMA) <= 18.0 pts` |

### Setup Grades & Vehicle Action
- **Score 5/5 (Grade A+)**: High-conviction expansion $\rightarrow$ Buy Calls / Call Debit Spread (CDS) or Buy Puts / Put Debit Spread (PDS).
- **Score 4/5 (Grade A)**: Trend confirmation $\rightarrow$ Sell Put Credit Spread (PCS) below lows or Sell Call Credit Spread (CCS) above highs.
- **Score 3/5 (Grade B)**: Far OTM credit spreads / Caution.
- **Neutral Chop**: Inside ORB range $\rightarrow$ Iron Condor or Cash.

---

## 3. Configuration Reference (`config/strategies.yaml`)

```yaml
  - name: DRMA_HM_SPX
    enabled: true
    confirmed: true
    module: strategies.drma_hm.strategy
    instrument: SPX
    broker: tastytrade
    stop_profile: drma_hm_strategy
    scheduled: false
    paper: true
    config_overrides:
      trade_mode: both               # 'both', 'directional_only', 'non_directional_only'
      min_grade_to_signal: 'Grade A and A+'
      # Non-directional parameters (Credit Spreads / Iron Condor)
      non_dir_selection_mode: delta  # 'delta' or 'credit'
      non_dir_target_delta: 0.15     # e.g. 0.15 delta
      non_dir_target_credit: 1.00    # e.g. $1.00 credit
      non_dir_spread_width: 10
      non_dir_profit_target_pct: 0.50 # 50% credit PT
      non_dir_stop_loss_pct: 1.00    # 100% credit SL (2x credit)
      # Directional parameters (Debit Spreads / Long Options)
      dir_vehicle_type: debit_spread # 'debit_spread' or 'long_option'
      dir_selection_mode: delta      # 'delta' or 'debit'
      dir_target_delta: 0.40         # e.g. 0.40 delta
      dir_target_debit: 2.50         # e.g. $2.50 debit
      dir_spread_width: 10
      dir_profit_target_pct: 0.50    # 50% debit PT
      dir_stop_loss_pct: 0.50        # 50% debit SL
      # Moving Average and Signal Filters
      ma_type: EMA
      ma_len: 21
      min_slope_pts: 0.5
      max_extension_pts: 18.0
      orb_minutes: 30
      wait_for_orb: true
      filter_lunch_chop: false
      enable_debounce: true
      cooldown_bars: 5
      max_daily_signals: 4
      force_eod_exit: true
      eod_exit_time_cst: '14:50'
      exit_style: trend_runner
      min_hold_bars: 3
      # Data feed & Overrides (Overnight & Prior Day)
      enable_yahoo_finance: true
      quantity: 1
```

---

## 4. Test Suite Summary

Unit and simulation tests in [`tests/test_drma_hm.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/tests/test_drma_hm.py):
- `test_daily_reset_ma_and_orb`: Verifies daily reset EMA and 30m ORB formation boundary.
- `test_rsi_and_wma`: Verifies Wilder's RSI-9, Fast EMA-3, Slow WMA-21.
- `test_exit_signal_evaluation`: Verifies 21 EMA trail, H-M reversal, and micro-hook exit styles.
- `test_select_credit_spread_by_delta` & `by_credit`: Verifies PCS and CCS strike selection.
- `test_select_directional_spread_by_delta` & `by_debit`: Verifies CDS and PDS strike selection.
- `test_manual_levels_priority`: Verifies manual overrides priority over remote feeds.
- `test_backtest_simulation_on_recorded_csv`: Simulates entire October 8th session on `data/2026-10-08/SPX_5m.csv`, validating that signals fire at the exact institutional breakout window.
