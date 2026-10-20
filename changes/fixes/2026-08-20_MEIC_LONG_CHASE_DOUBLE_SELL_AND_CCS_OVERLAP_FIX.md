# Design & Implementation: MEIC Long Chase Double-Sell Prevention, Exponential Backoff & CCS Overlap Resilience

**Date:** 2026-08-20 / 2026-08-21  
**Category:** Incident Fix & Hardening  
**Components:** `blocks/stop/monitor.py`, `blocks/stop/v3/supervisor.py`, `brokers/base.py`, `brokers/tastytrade_broker.py`, `common/strike_guard.py`, `blocks/entry/spread_scan.py`  
**Status:** Implemented & Verified with Unit Tests

---

## 1. Executive Summary & Problem Statements

During the live session on **2026-08-20**, three distinct issues occurred during high market volatility in SPX 0DTE trading:

1. **7615 Long Put Chase Loop & Account Margin Spikes**:
   - `12-45_P` and `10-45_P` stops triggered. The supervisor attempted to close `.SPXW260820P7615` via `_chase_long_close()`.
   - Repeated order placement rejections occurred due to broker margin checks (`margin_check_failed`), resulting in an infinite rapid retry loop (thousands of requests) that caused CPU and API churn until manually stopped.
2. **7620 Long Put Double-Sell (Naked Short Creation)**:
   - When `.SPXW260820P7620` (`11-15_P`) was being closed, `_chase_long_close()` sent a cancel request and immediately placed a replacement sell order on the exact same tick.
   - On attempt 10, the code also executed both a limit order and a market order concurrently.
   - Because cancellation at the exchange is asynchronous, the original order filled while the replacement order also executed, resulting in **2 sold contracts** for an account holding only **1 long contract**.
   - This created an unintended **-1 Naked Short 7620 Put**, which exhausted account buying power and caused TastyTrade to block all subsequent 7615 long put closes with margin errors.
3. **12-45 CCS Tranche Entry Failure (Strike Clash & Shift Trap)**:
   - At 12:44 CT, `12-45_C` scanned for a Call Credit Spread. The primary candidate `7680/7705 C` collided with the existing short leg `7705 C` of `10-45_C`.
   - Strike guard shifted the candidate $5 down toward ATM (`7675/7700 C`). Because shifting a CCS toward ATM increases credit, the credit exceeded `credit_max` ($1.20 + 10% = $1.32).
   - The scanner rejected the shifted spread as out of band and broke early without evaluating remaining widths (30, 35) or shifting further OTM, causing the entire tranche to fail after 3 retries.

---

## 2. Technical Design & Architecture

```mermaid
flowchart TD
    subgraph Long_Chase_Safety[Strict Cancel-Before-Replace & Position Pre-Check]
        A[Chase Tick Triggered] --> B{Order ID active?}
        B -- Yes --> C[Query Broker Order Status]
        C --> D{Status?}
        D -- filled --> E[Record Fill & Finalize Closed]
        D -- cancelled/rejected --> F[Clear OID -> Place Next Limit/Market]
        D -- working & repricing needed --> G[Send Cancel Request -> DO NOT send new order on same tick]
        G --> H[Wait for Next Tick to Confirm Cancelled]
        B -- No --> I{Held Quantity <= 0?}
        I -- Yes --> J[Skip Broker Order & Auto-Finalize Closed]
        I -- No --> K[Place Order clamped to min(desired, held)]
    end

    subgraph Entry_Overlap_Resolution[Bi-Directional Shift & Multi-Width Search]
        L[Scan Spread Grid] --> M{Collision Detected?}
        M -- No --> N[Return Clean Candidate]
        M -- Yes --> O[Try Default Shift: CCS down, PCS up]
        O --> P{In Credit Band?}
        P -- Yes --> Q[Return Shifted Candidate]
        P -- No --> R[Try Reverse Shift: CCS up, PCS down further OTM]
        R --> S{In Credit Band?}
        S -- Yes --> T[Return Reverse Shifted Candidate]
        S -- No --> U[Proceed to Next Width 30, 35]
        U --> L
    end
```

---

## 3. Detailed Implementations

### A. Long Chase Order Cancellation & Safety (`blocks/stop/monitor.py`)
1. **Cancel-Before-Replace**:
   - When repricing or escalating an active order, `cancel_order(oid)` is issued.
   - The method returns immediately without placing a replacement order on the same tick.
   - On the subsequent supervisor cycle:
     - If `get_order_status(oid)` returns `filled`: Fill price is recorded and trade status moves to `closed`. Zero secondary orders are submitted.
     - If `get_order_status(oid)` returns `cancelled` / `rejected`: `long_close_order_id` is cleared and the new limit or market order is safely submitted.
2. **Single-Leg Broker Position Pre-Check**:
   - In `_place_long_close_limit()` and `_place_long_close_market()`, `broker.get_single_leg_position_qty(long_symbol)` is called before placing any broker order.
   - If `held_qty <= 0`: Logs warning and immediately calls `_finalize_close()`, skipping broker order submission.
   - If `held_qty > 0`: Sets `qty = min(stop_qty_for_state(self.state), held_qty)`, guaranteeing no over-selling.
3. **Exponential Backoff & Fail Tracking**:
   - Placement errors record `long_close_fail_count`, `long_close_last_error`, and `long_close_next_retry_at`.
   - Backoff starts at 5s and doubles up to 60s max ($5\text{s} \to 10\text{s} \to 20\text{s} \to 40\text{s} \to 60\text{s}$).
   - Both `_chase_long_close()` and `StopSupervisor._poll_closing()` respect `long_close_next_retry_at`.

### B. Broker Position API (`brokers/base.py` & `brokers/tastytrade_broker.py`)
- Added `get_single_leg_position_qty(symbol: str) -> int`:
  - Queries `account.get_positions()` from TastyTrade REST API.
  - Returns net integer quantity held (positive for long, negative for short, 0 for flat).

### C. Multi-Width Scanning & Bi-Directional Shift (`common/strike_guard.py` & `blocks/entry/spread_scan.py`)
1. **Bi-Directional Shift**:
   - `shift_spread_strikes()` and `resolve_leg_overlap()` support `direction='default'` (toward ATM) and `direction='reverse'` (further OTM).
   - In `_resolve_overlap_candidate()`, if shifting toward ATM pushes credit above `credit_max`, it automatically tests the reverse shift further OTM to see if credit remains $\ge credit\_min$.
2. **Nested Width Evaluation**:
   - In `scan_credit_spreads()`, candidate scanning iterates width-by-width (`for width in widths: for otm in otm_values:`).
   - When a width drops below `credit_min`, `break` only exits that specific width's OTM sweep, ensuring wider spreads (e.g. 30, 35) are fully evaluated rather than aborting the scan.
3. **Duplicate Vertical Spread Guard**:
   - Enhanced `leg_overlap_conflict()` to detect when both short and long strikes match an existing open spread on the same side, preventing identical risk duplication.

---

## 4. Files Modified

| File | Primary Changes |
|---|---|
| `blocks/stop/monitor.py` | Strict cancel-before-replace, position pre-checks before STC order placement, exponential backoff, attempt 10 fix |
| `blocks/stop/v3/supervisor.py` | Cooldown gating in `_poll_closing` based on `long_close_next_retry_at` |
| `brokers/base.py` | Added abstract `get_single_leg_position_qty(symbol: str) -> int` |
| `brokers/tastytrade_broker.py` | Implemented `get_single_leg_position_qty` via `account.get_positions()` |
| `common/strike_guard.py` | Added bi-directional shift and duplicate vertical spread protection |
| `blocks/entry/spread_scan.py` | Nested width scanning loop, bi-directional overlap shift fallback |
| `tests/mock_broker.py` | Added mock single leg position tracking |
| `tests/test_long_close_chase.py` | Unit tests for cancel-before-replace, race condition fill, and 0-qty skip |
| `tests/test_strike_guard.py` | Unit tests for reverse shift and duplicate spread detection |
| `tests/test_spread_scan_pick.py` | Unit tests for multi-width continuation on overlap collision |

---

## 5. Verification & Test Results

```bash
uv run pytest tests/test_long_close_chase.py tests/test_strike_guard.py tests/test_spread_scan_pick.py -q
# 30 passed in 2.76s

uv run pytest -k "v3 or chase or strike or spread or stop or breach" -q
# 272 passed
```

### Key Test Cases Verified:
1. `test_chase_steps_down_when_mid_matches_working_limit`: Confirms 2-tick lifecycle (Tick 1 cancel, Tick 2 replace).
2. `test_chase_cancel_race_filled_does_not_double_sell`: Confirms that an in-flight fill during cancel immediately closes trade without sending duplicate order.
3. `test_place_long_close_limit_skips_when_flat` & `test_place_long_close_market_skips_when_flat`: Confirms no sell orders are placed when position is $\le 0$.
4. `test_scan_evaluates_wider_width_when_narrower_width_has_overlap_and_breaks`: Confirms scanner widens from 25 to 30 when 25 has unresolved overlap.
5. `test_resolve_leg_overlap_reverse_direction`: Confirms CCS shifting further OTM to clear collision.
