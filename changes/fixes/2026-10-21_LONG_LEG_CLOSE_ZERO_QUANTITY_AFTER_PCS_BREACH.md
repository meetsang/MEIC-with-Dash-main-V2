# Long Leg Close 0 Quantity Fix After Manual PCS Breach

- **Date**: 2026-10-21
- **Category**: Bug Fix & Execution Safeguard
- **Affected Strategy**: Manual Credit Spreads (`MANUAL_SPREAD` / PCS) & All Multi-Leg Exit Strategies
- **Affected Positions**: `ms-481_P` (Short 7660P / Long 7635P)
- **Affected Components**:
  - [`blocks/stop/fill_sync.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/fill_sync.py)
  - [`blocks/stop/monitor.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/monitor.py)
  - [`blocks/stop/v3/trade_slot.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/v3/trade_slot.py)
  - [`tests/test_partial_fill_stops.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/tests/test_partial_fill_stops.py)
  - [`tests/test_stop_fill_long_close.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/tests/test_stop_fill_long_close.py)
- **Status**: Implemented & Verified with Automated Test Suite

---

## 1. Problem Statement & Incident Context

On 2026-10-21, manual put credit spread `ms-481_P` (3 contracts on SPX 7660P / 7635P) was breached and the short 7660P leg stopped out / closed at broker.

When `StopMonitor` transitioned to `status: closing` and initiated `_chase_long_close()`, TastyTrade repeatedly rejected `SELL_TO_CLOSE` single-leg limit orders with:
```
legs[0][quantity]: must be a positive number
```
The exit routine retried 38 times with exponential backoff because it was sending `quantity=0`.

---

## 2. Root Cause Analysis

### Yesterday's Fix (2026-10-20)
On 2026-10-20, we resolved an issue where an exchange stop at the broker only partially filled (e.g. 2 of 3 contracts). To properly track remaining short contracts and prevent over-closing on subsequent breach replacements, `stop_qty_for_state(state)` was updated:
- If `state.get('short_remaining_qty') is not None`: return `int(state['short_remaining_qty'])`
- When the short leg is fully closed or stopped out, `state['short_remaining_qty']` is set to `0`.

### Today's Bug Origin
In [`blocks/stop/monitor.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/monitor.py):
`_place_long_close_limit()` and `_place_long_close_market()` determined order quantity via:
```python
desired_qty = stop_qty_for_state(self.state)
```
Because `stop_qty_for_state()` is specifically designed for the **short-leg** exchange stop:
1. When the short leg finished closing, `short_remaining_qty` was set to `0`.
2. When the long close routine started, `stop_qty_for_state(self.state)` returned `0`.
3. `desired_qty` became `0`, and without a `<= 0` guard, `place_limit_order('SELL_TO_CLOSE', long_sym, 0, limit_price)` was sent to TastyTrade.
4. TastyTrade rejected order quantity `0`.

---

## 3. Implemented Resolution

### A. Dedicated Long Leg Quantity Helper ([`blocks/stop/fill_sync.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/fill_sync.py))
Separated long leg accounting from short leg stop accounting with `long_qty_for_state(state)`:
- Returns `0` if `status == 'closed'`.
- Returns explicit `long_remaining_qty` if set.
- Calculates remaining quantity as `total_entry - already_closed_longs`, where:
  - `total_entry = holding_quantity or filled_quantity or quantity`
  - `already_closed_longs = sum(p['qty'] for p in long_partial_fills)`
- Is completely independent of `short_remaining_qty`.

### B. Updated Monitor Long Close Routines ([`blocks/stop/monitor.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/monitor.py))
1. In `_place_long_close_limit()` and `_place_long_close_market()`:
   - Calculate `desired_qty = long_qty_for_state(self.state)`.
   - Clamped by `held_qty` from broker position check (type-safe check `isinstance(pos, (int, float))`).
   - Added guard: if `qty <= 0`, auto-finalize close as `position_flat` instead of submitting `0` to the broker.
2. In `_close_long_leg(quantity)`:
   - When `quantity` is explicitly provided, records `self.state['long_remaining_qty'] = int(quantity)` rather than ignoring it.
3. In `_chase_long_close()`:
   - Type-safe check on broker positions.
   - When order is cancelled with partial fill (`result.filled_quantity > 0`), appends to `long_partial_fills`.
   - When fully filled, calculates VWAP blended fill price via `_calculate_blended_long_close_price()`.
   - If `long_qty_for_state() <= 0`, finalizes close cleanly.
4. In `_finalize_close()`:
   - Explicitly zeroes both `short_remaining_qty` and `long_remaining_qty`.
5. In `_maybe_merge_disk_stop_state()` & [`blocks/stop/v3/trade_slot.py`](file:///c:/Users/User/OneDrive/Desktop/Projects/2026/MEIC-with-Dash-main-V2/blocks/stop/v3/trade_slot.py):
   - Merges `short_remaining_qty`, `short_partial_fills`, `long_remaining_qty`, and `long_partial_fills` across in-memory state and disk.

---

## 4. Test Verification

Added and verified comprehensive unit tests:
1. `test_long_qty_for_state_unaffected_by_short_remaining_qty_zero`: Confirms `long_qty_for_state` returns 3 when `short_remaining_qty == 0`.
2. `test_long_qty_for_state_partial_fills_and_closed_status`: Confirms deduction of `long_partial_fills` and returns 0 when closed.
3. `test_long_close_places_correct_quantity_after_pcs_breached`: Replays today's `ms-481_P` breach scenario, verifying `SELL_TO_CLOSE` is placed for 3 contracts (not 0).
4. `test_long_close_zero_quantity_auto_finalizes_safely`: Verifies auto-finalization without broker error if position is flat.
5. `test_stop_filled_defers_long_close_until_delay_elapsed`: Validates existing 5-contract stop fill places 5 long contracts.
