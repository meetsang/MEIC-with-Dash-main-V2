# Dale Perryman NFH SPX 0DTE Iron Fly Scaled to XSP (Mini-SPX) Implementation

**Date:** 2026-10-03  
**Status:** Implemented, Integrated & Fully Tested  
**Test Suite:** `pytest tests/test_iron_fly_xsp_*.py` → 21 passed (70 passed across all strategy & integration tests)  
**Strategy ID:** `IRON_FLY_XSP`  
**Underlying:** `XSP` (Mini-SPX index options, cash-settled, European-style, 1/10th scale of SPX)

---

## 1. Problem & Core Requirement

The Dale Perryman NFH SPX 0DTE Iron Fly strategy trades full-size SPX index options with strikes spaced in 5-point increments (e.g. 5800, 5805, 5810, 7750, 7755).
When scaling down to Mini-SPX (XSP) to allow smaller account sizes and fine-grained risk management:
1. **Strike Increment Discrepancy:**
   - SPX strikes end in 0 or 5 (e.g., 7750, 7755).
   - In XSP (1/10th of SPX), 7750 scales cleanly to 775.0, but 7755 scales to **775.5**.
   - **XSP does not have 0.5-strike increments**; XSP options only trade on whole integer strikes (775, 776, etc.).
2. **The 2-Fly Synthetic Center Solution:**
   - If an SPX trigger yields a center ending in 5 (e.g. 7755 -> 775.5):
     - The strategy executes **a pair of 2 XSP flies per trigger**:
       - Fly A centered at $\lfloor 775.5 \rfloor = 775$ (qty 1)
       - Fly B centered at $\lceil 775.5 \rceil = 776$ (qty 1)
     - The effective short center of the position is $\frac{775 + 776}{2} = 775.50$, perfectly matching the synthetic SPX center.
   - If an SPX trigger yields a clean integer center (e.g. 7750 -> 775.0):
     - The strategy executes **2 flies both centered at 775** (qty 1 each).
     - Average center is 775.0.
   - **Result:** Exactly 2 XSP flies (2 contracts) are executed per trigger in all scenarios, scaling notional exposure down to 0.20x of SPX (2 XSP = $2 \times 1/10\text{th} = 1/5\text{th}$ SPX).
   - Long wings scale down by 10 to integer strikes (e.g. 30 pt SPX width -> 3 pt XSP width).

---

## 2. Architecture & Modules

```
strategies/iron_fly_xsp/
├── __init__.py           # Exports IronFlyXspStrategy, IronFlyXspConfig
├── rules.py              # Pure math, XSP center pairing, decay tiers, BE cuts, intrinsic
├── orders.py             # 4-leg combos with .XSP OCC symbols, tick rounding
├── book.py               # IronFlyXspPair and IronFlyXspBook atomic JSON state management
├── executor.py           # PaperExecutorXsp and BrokerExecutorXsp
└── strategy.py           # IronFlyXspStrategy runner (dual 15s/2s cadences, spot derivation)
```

### 2.1 Pure Rules Engine (`strategies/iron_fly_xsp/rules.py`)
- `get_xsp_centers(spx_center: int) -> Tuple[int, int]`:
  - `spx_center % 10 != 0` (e.g. 7755): returns `(floor(775.5), ceil(775.5)) = (775, 776)`.
  - `spx_center % 10 == 0` (e.g. 7750): returns `(775, 775)`.
- `evaluate_nfh_trigger_xsp(closest_center, closest_credit, current_spot, current_time, ...)`:
  - Scaled NFH triggers based on closest open IB's opening credit:
    - Credit > $2.00 and ITM > $1.50 -> Add new IB centered ±$2.00 away (effective short put ±$0.25 from underlying).
    - Credit $1.50–$2.00 and ITM > $1.00 -> Add new IB centered ±$1.50 away.
    - Credit < $1.50 and ITM > $0.75 -> Add new IB centered ±$1.00 away.
    - Tight Mode (after 2:00 PM ET or 50-wide mid < $1.00): ITM > $0.25 -> Add new IB centered ±$0.50 away.
- `pick_widths_xsp(...)`:
  - Initial fly: strictly 5-wide wings ($50-wide scaled /10).
  - Additional flies: target ~$0.04 mid on long put wing, bounded between min 2 pt and max 5 pt widths.
  - Symmetrically matches call wing unless existing short strike clash forces step-out.
- `should_cut_beyond_breakeven_xsp(...)`:
  - Spot moves beyond breakeven by 0.001 (or base buffer): `spot <= be_low - 0.001` or `spot >= be_high + 0.001`.
  - Supports scaled PABBE, Late BBE (3:30 PM), EOD BBE (3:50 PM), and Return on Risk guard (`Min Close RoR` -90%).


### 2.2 Orders & Symbol Protocol (`strategies/iron_fly_xsp/orders.py`)
- Symbols formatted as `.XSP{yymmdd}{C|P}{strike}`.
- 4-leg Net Credit combo orders for opening, Net Debit combo orders for closing.
- Combo tick rounding: floor to 0.05 for credit, ceil to 0.05 for debit.

### 2.3 State Book & Lifecycle (`strategies/iron_fly_xsp/book.py`)
- `IronFlyXspPair`:
  - Encapsulates `fly_a` and `fly_b`.
  - Computes `credit_total`, `credit_avg`, `be_low`, `be_high`, `unrealized_pnl`, `realized_pnl`, `fees_paid`, and `open_risk`.
- `IronFlyXspBook`:
  - Manages active ladder and history under `trades/active/iron_fly_xsp/` and `trades/history/iron_fly_xsp/`.
  - Full compatibility with `common/manual_close.py` via `structures` serialization.
  - Event diary with audit log.

### 2.4 Strategy Orchestrator (`strategies/iron_fly_xsp/strategy.py`)
- Inherits from `BaseStrategy(ABC)`.
- Implements `run()`, `stop()`, `start()`, `on_tick()`, and `get_status()`.
- Dual execution cadence:
  - 15-second main loop for EM calculation, ladder triggers, and pair entry.
  - 2-second background thread (`_spot_cut_loop`) for real-time break-even monitoring.
- Spot Derivation:
  - Uses direct `.XSP` index price if available.
  - Automatically derives $XSP = SPX / 10.0$ if `.XSP` is absent.
- Safety Limits:
  - Max open risk: $3,000 (scaled from $15,000 SPX).
  - Daily loss stop: $500 (scaled from $2,500 SPX).
  - Max pairs opened per day: 20 (40 contracts).

---

## 3. Dashboard Integration

- **Navigation & Views** (`dashboard/templates/index.html`):
  - Top P&L stat card `#pnl-iron-fly-xsp-today`.
  - Dedicated tab item **Iron Fly XSP**.
  - Dashboard panel `#strategy-iron-fly-xsp` with:
    - Active Paired Ladder table with Ideal Center, Fly A (Floor) & Fly B (Ceil) strikes, Credits, BE bounds, Live Distance to BE, and P&L.
    - Pending Pair Approvals table for `LIVE_APPROVE` mode.
    - Activity Diary with timestamped logs.
    - Emergency "Close All Open XSP Flies" button.
- **Backend API Endpoints** (`dashboard/server.py`):
  - `GET /api/iron_fly_xsp/today`: Returns complete active book, day's P&L, pairs, diary events, and historical performance.
  - `POST /api/iron_fly_xsp/approve`: Operator approval for pending live entry pairs.
  - `POST /api/iron_fly_xsp/skip`: Operator rejection of pending live entry pairs.
  - `POST /api/iron_fly_xsp/close_all`: Closes all open XSP flies using `common/manual_close.py`.
  - P&L included in top summary via `_iron_fly_xsp_today_pnl()`.

---

## 4. Verification & Testing

The implementation was validated with 70 passing unit tests:
```bash
.venv\Scripts\python.exe -m pytest \
    tests/test_iron_fly_xsp_rules.py \
    tests/test_iron_fly_xsp_book.py \
    tests/test_iron_fly_xsp_strategy.py \
    tests/test_iron_fly_xsp_dashboard.py \
    tests/test_iron_fly_*.py \
    tests/test_strategy_loader.py \
    tests/test_stop_profile.py
```
- **Rules (`tests/test_iron_fly_xsp_rules.py`):** Verified fractional strike split (7755 -> 775 & 776), integer center duplicate (7750 -> 775 & 775), width scaling, clash prevention, pricing, BE cut math, and intrinsic cash settlement.
- **Book (`tests/test_iron_fly_xsp_book.py`):** Verified pair creation, atomic JSON persistence, reload, and `structures` schema compatibility.
- **Strategy (`tests/test_iron_fly_xsp_strategy.py`):** Verified fractional and integer pair execution, background 2s spot-cut breach trigger, and $3,000 risk cap enforcement.
- **Dashboard (`tests/test_iron_fly_xsp_dashboard.py`):** Verified REST endpoints, P&L aggregation, and close-all operations.
