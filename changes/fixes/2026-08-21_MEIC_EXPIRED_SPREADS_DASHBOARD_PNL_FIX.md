# Design & Implementation: MEIC Expired Spreads Table View & Day P&L Settlement Fix

**Date:** 2026-08-21  
**Category:** Bug Fix, Dashboard PnL & Settlement Accuracy  
**Components:** `dashboard/server.py`, `dashboard/templates/index.html`, `dashboard/db.py`, `tests/test_dashboard_meic_pnl.py`  
**Status:** Implemented, Verified & Numbers Corrected for 2026-08-21

---

## 1. Problem Statement & Root Cause Analysis

### Symptoms
1. After MEIC completed for the day (2026-08-21), expired credit spreads (which expired 100% worthless for full credit profit) appeared as empty dashes (`–`) in the Tranche Grid tabular view.
2. The dashboard Day P&L showed **-$285.00** instead of the actual **+$430.00** (+ $120.00 PermaBull = **+$550.00** Live total).
3. The closed trade count showed `3 closed` (only counting stopped trades) instead of `10 closed`.

### Root Cause
1. **Frontend Exclusion in `spreadRowHasData()` (`dashboard/templates/index.html`)**:
   - `spreadRowHasData(s)` only checked for `['open', 'closing', 'breached', 'working', 'closed', 'killed']`.
   - When trades expired at 3:00 PM CT, `_slot_state_from_trade()` set their state to `'expired'`.
   - Because `'expired'` was missing from `spreadRowHasData()`, `gridRowHasData()` returned `false` for every expired spread. The UI rendered empty dashes (`–`) for all cells (Entry, Live P&L, Slippage, Qty, etc.) and omitted their PnL from the grid totals row.
2. **Backend Exclusion in `MEIC_GRID_PNL_STATES` (`dashboard/server.py`)**:
   - `MEIC_GRID_PNL_STATES = frozenset({'open', 'closing', 'breached', 'working', 'closed', 'killed'})` did not include `'expired'`.
   - `_counts_toward_meic_day_pnl()` returned `False` for any slot with display state `'expired'`.
   - Consequently, `build_summary()` completely skipped adding the realized profit of the 7 expired spreads (`+$715.00`), only summing the 3 stopped spreads (`-$285.00`).
3. **Database Test Artifact**:
   - A mock trade from `test_long_close_chase.py` (`11-00_C`, id 544) had inadvertently been recorded into `trades.db`.

---

## 2. Correct Numbers for Today (2026-08-21)

| Tranche | Side | Status / Close Mechanism | Net Credit | Exit Debit | Realized P&L |
| :--- | :---: | :--- | :---: | :---: | :---: |
| **11-15** | P | `closed (expiry_settlement)` | $1.10 | $0.00 | **+$110.00** |
| **11-15** | C | `closed (exchange_stop)` | $1.00 | $2.05 | **-$105.00** |
| **11-45** | P | `closed (expiry_settlement)` | $1.05 | $0.00 | **+$105.00** |
| **11-45** | C | `closed (expiry_settlement)` | $1.05 | $0.00 | **+$105.00** |
| **12-15** | P | `closed (expiry_settlement)` | $1.30 | $0.00 | **+$130.00** |
| **12-15** | C | `closed (expiry_settlement)` | $0.90 | $0.00 | **+$90.00** |
| **12-45** | P | `closed (expiry_settlement)` | $1.20 | $0.00 | **+$120.00** |
| **12-45** | C | `closed (exchange_stop)` | $0.65 | $1.30 | **-$65.00** |
| **01-30** | P | `closed (expiry_settlement)` | $0.55 | $0.00 | **+$55.00** |
| **01-30** | C | `closed (exchange_stop)` | $1.15 | $2.30 | **-$115.00** |
| **MEIC Day Subtotal** | — | **10 Trades (7 Wins, 3 Losses)** | — | — | **+$430.00** |
| **PermaBull PCS (Live)** | P | `closed (profit_target_limit)` | $1.35 | $0.15 | **+$120.00** |
| **Total Live Day P&L** | — | **11 Trades (8 Wins, 3 Losses)** | — | — | **+$550.00** |

---

## 3. Implementations & Changes

1. **`dashboard/server.py`**:
   - Added `'expired'` to `MEIC_GRID_PNL_STATES`.
   - `build_summary()` and `_counts_toward_meic_day_pnl()` now correctly count and sum expired trades toward `day_pnl` and `closed_count`.
2. **`dashboard/templates/index.html`**:
   - Added `'expired'` to `spreadRowHasData(s)`.
   - The grid table now displays full entry prices, $0.00 exit prices, and full green P&L numbers for all expired tranches.
3. **History Tab Live Money Default & PermaBull Integration (`dashboard/server.py`, `dashboard/db.py`, `dashboard/templates/index.html`)**:
   - History tab was previously defaulting `moneyFilter` to `All $ (live+paper)`, which combined the **+$550.00 Live trading profit** with the **-$780.00 Paper Skyline simulated trade**, showing a blended `-$230.00`.
   - Defaulted History tab money filter to `Live $` (`money=live`) on both dropdowns so the trading calendar, stats cards, and chart display real live trading performance (+ $550.00 for 2026-08-21) by default.
   - Updated `infer_strategy` and `get_daily_breakdown` in `dashboard/db.py` and `api_daily_calendar` in `dashboard/server.py` to recognize `STRATEGY_PERMA_BULL` (`PERMA_BULL_PCS`).
   - Updated `trades.db` so `perma-2026-08-21` is properly tracked under `PERMA_BULL_PCS`.
4. **Trade History Table High-Contrast Dark Mode (`dashboard/templates/index.html`)**:
   - Added dedicated `.history-table` styles: `#181c28` headers, `#1a1e2b`/`#202536` alternating row backgrounds, `#f1f5f9` cell text, bright short/long strike colors (`#38bdf8` / `#cbd5e1`), and vibrant status badges.
5. **Unit Tests (`tests/test_dashboard_meic_pnl.py`)**:
   - Updated tests to verify that `expired` state is in `MEIC_GRID_PNL_STATES` and counts toward MEIC day P&L.

---

## 4. Verification

```bash
uv run pytest tests/test_dashboard_meic_pnl.py tests/test_phase6_dashboard_runtime_status.py tests/test_manual_dashboard_trades.py tests/test_probe_coordinator.py tests/test_entry_runner.py tests/test_long_close_chase.py tests/test_strike_guard.py tests/test_spread_scan_pick.py
# 59 passed in 7.22s
```
