# MEIC Incident Report & Comprehensive Resolution Plan
**Date of Incident**: 2026-08-20  
**Affected Strategy**: MEIC 0DTE Credit Spreads  
**Affected Positions**: `.SPXW260820P7640` (Short 7640 Put) / `.SPXW260820P7615` (Long 7615 Put)  
**Tranches**: `10-45_P` and `12-45_P`  

---

## 1. Incident Executive Summary

On 2026-08-20, MEIC stopped out of the short 7640 put on tranche `10-45_P` and began the automated `long_chase` routine to liquidate the long 7615 put. During this exit sequence, TastyTrade repeatedly rejected `SELL_TO_CLOSE` single-leg orders with `HTTP 422 Unprocessable Entity: margin_check_failed`.

Because the stop monitor and V3 supervisor did not have an error backoff mechanism or circuit breaker on broker rejection during long chase, the supervisor immediately re-scheduled `resume_long_chase` on every tick cycle (~250ms). This created a rapid-fire loop (API storm) sending thousands of rejected requests to TastyTrade between 13:10 CT and 13:46 CT until the operator shut down the launcher and manually closed the 7615 put.

---

## 2. Root Cause Analysis

### A. TastyTrade 422 Margin Rejection on Single-Leg Long Close
1. **Duplicate Strikes**: At 10:44 CT (`10-45_P`) and 12:44 CT (`12-45_P`), both tranches opened the identical spread (7640/7615 Put Spread). The account held **2x Short 7640P** and **2x Long 7615P**.
2. **Account Margin State**: The account held multiple active structures (9 active MEIC credit spreads across calls and puts, plus Skyline SPX structures).
3. **Broker Margin Engine Behavior**: When the short 7640P stop filled, the tranche attempted to sell the long 7615P individually (`SELL_TO_CLOSE`). In TastyTrade's margin risk engine, submitting a single-leg order to sell a long option while other positions are open requires option buying power validation. Because buying power was tied up in other open tranches, TastyTrade rejected the sell order with `margin_check_failed`.

### B. Unbounded Tight Loop in Stop Supervisor
1. In `blocks/stop/monitor.py` (`_place_long_close_limit()`):
   - When `broker.place_limit_order()` returned `success=False`, it logged the error and returned `False`.
   - `long_close_order_id` remained `None`, and trade `status` remained `'closing'`.
   - No failure counter, backoff cooldown, or delay timestamp was set on the trade state.
2. In `blocks/stop/v3/supervisor.py` & `blocks/stop/v3/recovery.py`:
   - `resolve_exit_recovery_route()` saw `status: closing` with `short_closed_at` and routed to `resume_long_chase`.
   - `_poll_closing()` enqueued `LongChaseHandler` every cycle without checking if an error recently occurred.
   - `LongChaseHandler` invoked `_chase_long_close()`, which immediately retried `_place_long_close_at_mid()` without delay.

---

## 3. Resolution Plan

### Component 1: Exponential Backoff & Error Throttling in Long Chase
- **File**: `blocks/stop/monitor.py`
- **Logic**:
  - When `_place_long_close_limit()` fails:
    - Increment `long_close_fail_count`.
    - Record `long_close_last_error` and `long_close_failed_at`.
    - Set `long_close_next_retry_at = now + backoff_seconds` (e.g., 5s on 1st fail, 15s on 2nd, 30s on 3rd, 60s max).
  - In `_chase_long_close()`, check `long_close_next_retry_at`. If in backoff, skip placement and exit cleanly.

### Component 2: Supervisor Scheduling Cooldown Gate
- **File**: `blocks/stop/v3/supervisor.py`
- **Logic**:
  - In `_poll_closing()`: Before enqueuing `LongChaseHandler`, check if `long_close_next_retry_at` is in the future.
  - If in cooldown, do not spawn worker jobs or hit the broker API.

### Component 3: Broker Position Verification & Auto-Resolve
- **File**: `blocks/stop/monitor.py`
- **Logic**:
  - After 3 consecutive long close rejections, query broker live positions for the long symbol.
  - If the broker reports **0 quantity held** (position already closed at broker or closed manually by operator), finalize the trade as closed and terminate `long_chase`.

### Component 4: Duplicate Spread Strike Prevention
- **File**: `common/strike_guard.py` & `blocks/entry/spread_scan.py`
- **Logic**:
  - Extend strike overlap validation to detect when an incoming tranche matches the exact same short & long strike of an already active open tranche on the same side, and shift strikes by $5 to prevent stacking duplicate risk on the identical strike.

---

## 4. Current State Confirmation for Active Files

All active trade state files for 2026-08-20 have been verified:
- `10-45_P_20260820T104403.json`: Marked `status: closed`, `exit_last_step: finalized_closed`, `exit_audit: manual_close`.
- `12-45_P_20260820T124403.json`: Marked `status: closed`.
- All other active tranches (`10-45_C`, `11-45_C`, `12-15_C`) remain in their appropriate open/closed status.

When the MEIC program is started, it will **not** attempt to close the 7615 long put or send any orders for it.
