# DRMA Closing Order Verification, Mid-Chase Engine & Persistent Closing Management

**Date:** 2026-10-26  
**Status:** Implemented & Verified  
**Affected Components:**  
- `strategies/drma_hm/strategy.py` (`_chase_options_spread_close`, `_exit_trade`, `_manage_closing_trade`, `_finalize_closed_trade`)
- `strategies/drma_stocks/strategy.py` (`_load_active_trades`)
- `dashboard/server.py` (`/api/drma_hm/today`, `/api/drma_stocks/today`)
- `tests/test_drma_hm.py` (`TestDRMAHMClosingOrderChase`)

---

## 1. Problem & Incident Analysis

### 1.1 The Incident
- On **2026-10-26 at 10:50:04 CST**, trade `credit_bull_753fd75c` (7665/7655 Put Credit Spread on SPX in `DRMA_HM_SPX`) fired an exit signal:
  `exit_reason: "momentum_exit: 2-bar close below 21 EMA"`
- `_exit_trade()` submitted limit closing order ID `518330903` at `-1.05` debit to TastyTrade.
- **The Bug:** `_exit_trade()` evaluated `res = close_broker.place_multi_leg_order(...)`, and because TastyTrade accepted the order (`res.success == True`), the code **immediately** marked `trade['status'] = 'closed'`, moved the trade file to `trades/history/DRMA_HM_SPX/`, unlinked active tracking, and reset strategy position states to 0 (`self._credit_options_position_state = 0`).
- In reality, TastyTrade limit orders are working limit orders (`res.filled_price == None`, `status == 'live'`). As SPX drifted lower, the debit required to buy back the 7665/7655 PCS rose to ~1.45–1.60. The order `518330903` remained **working and unfilled** on the brokerage book while the algorithm completely abandoned it.
- **Live Account Position:**
  - `('SPXW  261026P07655000', Decimal('1'), 'Long')`
  - `('SPXW  261026P07665000', Decimal('1'), 'Short')`
  - The live position remained open on TastyTrade book unmanaged.

---

## 2. Immediate Incident Remediation

1. Connected to live TastyTrade account `5WT45050`.
2. Verified order `518330903` was active and working (`status='live'`).
3. Executed automated closing chase:
   - Cancelled order `518330903`.
   - Polled live option mids for `SPXW  261026P07665000` (mid 3.05) and `SPXW  261026P07655000` (mid 1.575–1.625).
   - Attempt 1: Placed at debit limit -1.40 (order `518376703`). Status after 4s: live (not filled). Cancelled `518376703`.
   - Attempt 2: Repriced to debit limit -1.45 (order `518376784`). Status after 4s: **FILLED at -1.45 debit**.
4. Re-checked TastyTrade positions: verified SPX 7665/7655 position is **100% FLAT** (`Remaining 7665/7655 positions: []`).
5. Updated `trades/history/DRMA_HM_SPX/drma_hm_2026-10-26_credit_bull_753fd75c.json` with the confirmed fill data:
   - `exit_price`: 1.45
   - `close_order_id`: "518376784"
   - `realized_pnl`: -0.38 pt (-$38.00)
   - `exited_at`: "2026-10-26T11:39:10"

---

## 3. Systematic Architecture & Solution

To prevent any closing order from being orphaned or prematurely assumed filled:

### 3.1 Dedicated Chase Engine (`_chase_options_spread_close`)
- Keeps checking brokerage if closing order is filled.
- Loop for up to `max_attempts` (default 5, `wait_sec=4.0`s):
  1. Checks direct order status via `close_broker.get_order_status_direct(oid)`.
  2. If `status == 'filled'` or `filled_quantity >= qty`, returns filled immediately.
  3. If still working:
     - Fetches fresh option mids using `close_broker.fetch_option_mids_api()`.
     - Normalizes symbols using `symbols_equivalent` to match OCC vs streamer symbols.
     - **Credit spread buyback:** calculates `spread_mid_debit = mid(short) - mid(long)`. Reprices up to mid (`max(spread_mid, curr_debit + 0.05)` rounded to 0.05 tick).
     - **Debit spread sale:** calculates `spread_mid_credit = mid(long) - mid(short)`. Reprices down to mid (`min(spread_mid, curr_credit - 0.05)` rounded to 0.05 tick).
     - Cancels current working order via `close_broker.cancel_order(current_oid)`.
     - Checks cancel race condition (if filled during cancel, treats as filled without submitting duplicate).
     - Submits new replacement order at updated mid limit.
  4. Returns `(True, order_id, fill_price)` if filled, or `(False, order_id, last_limit)` if still unconfirmed.

### 3.2 Persistent `closing` State
- If the order fills during the initial chase, `_finalize_closed_trade()` immediately settles the trade and moves it to history.
- If after `max_attempts` the order has not filled yet:
  - Trade status is transitioned to `trade['status'] = 'closing'`.
  - Trade record is updated with `close_order_id`, `close_limit_price`, `close_reason`, `close_started_at`.
  - The trade file **remains in active directory** (`trades/active/DRMA_HM_SPX/`).
  - Strategy position states (`has_open_credit_opts`, `_options_position_state`) remain **active** to strictly prevent opening new positions while an existing position is closing.

### 3.3 Main Loop Closing Management (`_manage_closing_trade`)
- In `run()` (regular session, overnight session, startup resume, and EOD cutoff):
  - Any trade with `status == 'closing'` is routed to `self._manage_closing_trade(root, t_path)`.
  - Queries broker status: if filled, finalizes to history and unlinks active file.
  - If still working: continues the chase with fresh mids and reprices towards market.
- In `dashboard/server.py`:
  - `status == 'closing'` is counted in active trades table and active unrealized PnL.

---

## 4. Verification & Testing

- Added `TestDRMAHMClosingOrderChase` in `tests/test_drma_hm.py`:
  1. `test_spread_close_fills_immediately`: verified immediate fill moves to history.
  2. `test_spread_close_chases_and_fills_on_attempt_2`: verified attempt 1 cancelled, repriced up to mid, resent, and filled on attempt 2.
  3. `test_spread_close_persists_closing_state_when_unfilled`: verified trade stays in active directory with `status: 'closing'` when unfilled.
  4. `test_manage_closing_trade_detects_fill_and_finalizes`: verified background manager picks up closing trade, confirms fill, and finalizes cleanly.
- Full test suite passed:
  ```bash
  uv run pytest tests/test_drma_stocks.py tests/test_drma_hm.py tests/test_drma_hm_dashboard.py -v
  # 78 passed in 22.07s
  ```
