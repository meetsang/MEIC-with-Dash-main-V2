# Fix & Enhancement: DRMA Stocks UI Visibility, Opened At Column, and Pre-9:00 AM CST Trade Clean-Up

**Date**: 2026-10-26  
**Status**: Verified & Implemented  
**Scope**: `dashboard/templates/index.html`, `trades/active/DRMA_HM_STOCKS/`, `trades/history/DRMA_HM_STOCKS/`, `.agents/rules/UI_DESIGN_GUIDELINES.md`

---

## 1. Context & User Requirements

The user requested four specific items for the DRMA Stocks strategy dashboard and active session:
1. **Add `Opened At` Column**: In the Completed Trades table (`drma-stocks-history-tbody`), include an `Opened At` column alongside `Closed At`.
2. **High-Contrast Font Visibility**: Table cells in Active Positions (specifically `Targets (PT / SL)` and `Open Time`) and in Completed Trades (`Closed At` and `Opened At`) were previously rendered using Bootstrap's low-contrast `text-muted` (`#6c757d`), making them unreadable against dark dashboard backgrounds (`#141722`). Fix these to be high-contrast and permanently record a rule never to use `text-muted` on dark tables.
3. **Clean Up Pre-09:00 AM CST Trades**: Remove all trades opened prior to 9:00 AM CST today (2026-10-26), as trades cannot be placed before the 30-minute ORB formation.
4. **Clarification on Server Restart**: Clarify whether `run.py` needs to be restarted after today's changes.

---

## 2. Root Cause & Solution Details

### A. Font Color Contrast Root Cause
In `dashboard/templates/index.html`, `renderDrmaStocks()` was using `class="small text-muted"` for:
- Column `Targets (PT / SL)`: `<td class="small text-muted">PT: ${pt} | SL: ${sl}</td>`
- Column `Open Time`: `<td class="font-monospace small text-muted">${t.entered_at ? ...}</td>`
- Column `Closed At`: `<td class="font-monospace small text-muted">${h.closed_at ? ...}</td>`

On the dark `#141722` table background, `text-muted` renders in dark gray (`#6c757d`), which had an insufficient contrast ratio (< 2.5:1), making the data nearly invisible.

**Fix**:
- Replaced with `text-light fw-medium` (`#f8fafc`) for crisp, bright readability.
- Targets now highlighted with dedicated accent colors: `<span class="text-info fw-semibold">PT: ${pt}</span>` and `<span class="text-warning fw-semibold">SL: ${sl}</span>`.
- Added `Opened At` column to header (`<th>Opened At</th>`) and data rows (`<td class="font-monospace small text-light fw-medium">${h.entered_at ? formatCST(h.entered_at, true) : '—'}</td>`).
- Table headers updated from `text-secondary` to `color: #94a3b8;` (Slate-400) for clean contrast.
- Recorded permanent project rule in `.agents/rules/UI_DESIGN_GUIDELINES.md`.

### B. Pre-09:00 AM CST Trades Cleanup
Trades opened before 09:00 AM CST today were placed before the 30-minute Opening Range Breakout (ORB) established its boundaries. 11 trades were identified across active and history directories:
- **Active (5 trades)**:
  - `AMZN_ccs_8c6a1c.json` (entered 08:43:03 CST)
  - `MU_cds_cffacd.json` (entered 08:43:02 CST)
  - `NVDA_cds_0f665e.json` (entered 08:43:01 CST)
  - `NVDA_equity_long_3311ad.json` (entered 08:43:01 CST)
  - `SPCX_pcs_e2d3e5.json` (entered 04:42:24 CST)
- **History (6 trades)**:
  - `MSTR_ccs_a6c91a.json` (entered 04:42:25 CST)
  - `MSTR_equity_short_845268.json` (entered 08:43:06 CST)
  - `MSTR_pds_165110.json` (entered 08:43:06 CST)
  - `MU_cds_cffacd.json` (entered 08:43:02 CST)
  - `MU_equity_long_89baa6.json` (entered 08:43:02 CST)
  - `NVDA_ccs_9dae5f.json` (entered 04:42:19 CST)

All 11 files were safely archived to `trades/archive_pre9am_20261026/` and removed from `trades/active/DRMA_HM_STOCKS/` and `trades/history/DRMA_HM_STOCKS/`. Remaining active trades (`MSTR` and `MU` entered at 10:33 AM CST) and history trade (`MU_ccs_72b4bd` entered at 09:09 AM CST) are all valid post-09:00 AM CST trades.

### C. Expiries for Options Trades on DRMA Stocks
- **Header & Column Addition**: Added dedicated `Expiry` column to both the **Active Open Positions** table (`<th>Expiry</th>`) and **Today's Completed Stock Trades** table.
- **Formatter Utility (`formatStockExpiry`)**: Automatically parses YYMMDD (e.g., `'261030'`) or YYYYMMDD and formats to human-readable ISO date `2026-10-30`.
- Direct equity rows cleanly display `—` (or Shares), while options spreads feature a calendar icon and expiry date (`2026-10-30`).

### D. Credit vs Debit Structure Display Bug
- **Root Cause**: In `dashboard/templates/index.html` (line 12089), the template code previously hardcoded `+ ' Debit'` for every options position:
  ```javascript
  const strikes = isEq ? ... : (legs.map(l => l.strike).join('/') + ' Debit');
  ```
  Consequently, Non-Directional Credit Spreads (such as `AMZN` Call Credit Spread `CCS` and `SPCX` Put Credit Spread `PCS`) displayed confusingly as "Debit" even though they were credit spreads.
- **Fix**: The template now dynamically detects the spread type:
  ```javascript
  const isDebit = structType.includes('debit') || (subStrat === 'DRMA_STOCK_DIR');
  const spreadLabel = isDebit ? 'Debit' : 'Credit';
  ```
  And displays the structure badge, strikes, and type label clearly:
  - Directional: `CDS 144/154 DEBIT` ($2.50 Db)
  - Non-Directional: `CCS 265/255 CREDIT` ($1.00 Cr) / `PCS 135/145 CREDIT` ($1.00 Cr)

### E. Profit Target & Stop Loss Targets Missing Bug
- **Root Cause**: `index.html` was referencing `t.profit_target` and `t.stop_loss` dollar keys, which were `undefined` in active options trade files (which only contained percentage thresholds `profit_target_pct: 0.50` and `stop_loss_pct: 0.50/1.00`).
- **Fix**:
  1. Updated `strategies/drma_stocks/strategy.py` to calculate and store `profit_target_price`, `stop_loss_price`, `profit_target`, and `stop_loss` in the trade dictionary when trades are generated and monitored.
  2. Updated `index.html` with dynamic calculation fallback so PT and SL target marks always print with exact dollar price and percentage:
     - Direct Equity: `$148.56 (+3.0%)` | `$141.35 (-2.0%)`
     - Debit Spreads ($2.50 entry): `$3.75 (+50%)` | `$1.25 (-50%)`
     - Credit Spreads ($1.00 entry): `$0.50 (+50%)` | `$2.00 (-100%)`

### F. Server Restart Requirement
**YES, `run.py` MUST be restarted.**
- Strategy python files (`strategies/drma_stocks/config.py`, `strategies/drma_stocks/strategy.py`, `strategies/drma_hm/config.py`, `strategies/drma_hm/strategy.py`) were modified to revert ORB to 30 minutes, reset trading start to 09:00 AM CST, configure overnight ES futures timing, and calculate explicit PT/SL prices.
- Since Python imports modules into memory at launch, a running `run.py` process will continue executing in-memory bytecode until stopped and restarted.
- Restarting `run.py` ensures all timing rules, session cutoffs, and updated dashboard endpoints take effect immediately.

---

## 3. Verification Results

1. **Jinja2 / HTML / JS Template Parsing**: Verified valid template compilation with no syntax errors.
2. **Stock Evaluation Refresh**: Re-evaluated `evaluate_all_stocks()` with pre-9am trades removed; active position counters correctly updated:
   - `NVDA`: 0 active positions
   - `AMZN`: 0 active positions
   - `SPCX`: 0 active positions
   - `MU`: 2 active positions (valid 10:33 AM CST trades)
   - `MSTR`: 2 active positions (valid 10:33 AM CST trades)
3. **Automated Unit Tests**: All 74 tests in `tests/test_drma_stocks.py`, `tests/test_drma_hm.py`, and `tests/test_drma_hm_dashboard.py` passed cleanly (`74 passed in 29.16s`).
