# Changes Since July 6, 2026

**Project:** MEIC-with-Dash-main-V2  
**Started:** 2026-07-06  
**Purpose:** Living changelog — every implementation, finding, and operator decision **from July 6 onward**.

Older history (V2 rewrite, pre-Jul-6 design) stays in the other `changes/` docs — see [README.md](README.md) for folder layout (`design/`, `strategies/`, `sessions/`, `fixes/`). **Strategy north star:** [design/STRATEGY_DESIGN_PATTERN.md](design/STRATEGY_DESIGN_PATTERN.md). **Add a new dated section at the top** whenever you ship fixes, discover issues, or make operator decisions.

---

## 2026-10-20 — Strategy Persistent Live / Paper Execution Modes & Trade Provenance Safety

**Status:** implemented, verified & unit-tested  
**Tests:** `uv run pytest tests/test_strategy_modes_persistence.py tests/test_drma_hm.py tests/test_drma_hm_dashboard.py` → 43 passed  
**Doc:** [fixes/2026-10-20_STRATEGY_PERSISTENT_LIVE_PAPER_MODES_AND_PROVENANCE_SAFETY.md](fixes/2026-10-20_STRATEGY_PERSISTENT_LIVE_PAPER_MODES_AND_PROVENANCE_SAFETY.md)

### What changed
- **Persistent Strategy Execution Store (`common/strategy_modes.py` & `trades/strategy_modes.json`):**
  - Added dedicated atomic persistent storage (`trades/strategy_modes.json`) for strategy and sub-strategy execution modes (`paper`, `dir_paper`, `non_dir_paper`, `futures_paper`).
  - Implemented `get_strategy_paper_mode()`, `set_strategy_paper_mode()`, and `delete_strategy_paper_mode()` with atomic temp-file write and retry mechanism.
  - Changes made in the dashboard or via API persist indefinitely across sessions, calendar date rollovers, and launcher restarts until explicitly changed back.
- **Hierarchical Broker Resolution (`common/broker_factory.py`, `strategies/loader.py`, `common/daily_overrides.py`):**
  - `is_strategy_paper()` checks today's session overrides, then persistent modes from `strategy_modes.json`, before falling back to `strategies.yaml`.
  - `_build_config()` and `effective_config()` overlay persistent modes so strategies are initialized with the operator's configured execution environment.
- **Trade Provenance Safety Guard (`common/manual_close.py`, `strategies/drma_hm/strategy.py`, `blocks/stop/broker.py`):**
  - Enforced strict trade provenance: an open trade's internal `trade['paper']` flag strictly governs its closing broker.
  - Even if a strategy or sub-strategy is flipped to LIVE mode while a paper trade is open, manual closes via dashboard or automated exits will NEVER send live orders to TastyTrade; they remain 100% on paper via `JournalBroker`.
  - `resolve_broker_for_trade()` in Stop Supervisor checks `state.get('paper')` before checking strategy configuration.
- **Dynamic In-Loop Reload (`strategies/drma_hm/strategy.py`):**
  - The DRMA runtime monitoring loop reloads persistent modes every poll cycle, picking up dashboard changes on the fly.
  - Ensured `_load_session_state()` always initializes and preserves the `overrides` dictionary.

### Findings / Learnings
- Storing execution mode overrides exclusively in daily session files (`session_{YYYY-MM-DD}.json`) caused settings to disappear whenever the calendar date rolled over.
- Passing `paper = _strategy_paper_flag(name)` to `close_structure()` was unsafe because it allowed the strategy's current mode to override an existing open trade's provenance. Prioritizing `trade.get('paper')` prevents real capital risk when closing paper positions.

### Validation
- [x] Dedicated test suite in `tests/test_strategy_modes_persistence.py` verifying persistent storage, independent sub-strategy modes, session rollover persistence, build config overlays, and trade provenance safety during manual close and strategy exit (8/8 passed).
- [x] Existing DRMA test suites in `tests/test_drma_hm.py` and `tests/test_drma_hm_dashboard.py` (35/35 passed).
- [x] Regression testing across `tests/test_broker_factory_multi_strategy.py` and `tests/test_standalone_runner.py`.

---

## 2026-10-20 — Stop Order Partial Fill Immediate Cancellation & Market Mid Limit Replacement

**Status:** implemented, verified & unit-tested  
**Tests:** `uv run pytest tests/test_partial_fill_stops.py` → 11 passed  
**Doc:** [fixes/2026-10-20_STOP_PARTIAL_FILL_CANCEL_AND_MID_LIMIT_REPLACE.md](fixes/2026-10-20_STOP_PARTIAL_FILL_CANCEL_AND_MID_LIMIT_REPLACE.md)

### What changed
- **Immediate Partial Fill Cancellation & Market Mid Replacement (`blocks/stop/monitor.py`):**
  - Added `handle_partial_stop_fill()`: As soon as a stop order partial fill is detected, the working stop order is immediately cancelled (waiting up to 3s confirmation).
  - Queried actual broker position via `get_option_position_quantity()` to deduce exact remaining short contracts and captured partial fill audit records in `short_partial_fills`.
  - If remaining contracts > 0, immediately places a `BUY_TO_CLOSE` limit order at current live **Market Mid** (`get_market_mid()`) for the remaining quantity with zero wait time.
  - If all contracts filled before or during cancellation, transitions directly to `closing` and calculates VWAP blended fill price.
  - Updated `replace_with_limit_close()` to clamp close quantity to broker's actual held short position (`min(qty, remaining_short)`), eliminating TastyTrade `cannot_close_more_than_existing_position` rejections.
  - Added `_calculate_blended_short_close_price()` for volume-weighted average price calculation across multiple partial fills.
- **Fill Sync & Stop Quantity Deduction (`blocks/stop/fill_sync.py`):**
  - Updated `stop_qty_for_state()` to account for closed contracts recorded in `short_partial_fills` and prioritize `short_remaining_qty`.
- **WebSocket Alert & Sync Handling (`blocks/stop/v3/supervisor.py`, `blocks/stop/monitor.py`):**
  - Updated `_drain_alert_fills()` in `StopSupervisor` to route partial fill WebSocket events immediately into `mon.handle_partial_stop_fill()`.
  - Updated `_sync_active_close_order()` and `_reconcile_active_stop_with_broker()` to intercept partial fills from REST status polls and trigger immediate cancellation and limit replacement.
- **Common Framework Compatibility:**
  - Common across automated MEIC (`MEIC_IC`) and manual discretionary spreads (`MANUAL_SPREAD`).

### Findings / Learnings
- In fast drops, stop orders at TastyTrade can partially fill leaving residual contracts working. Attempting to close using initial filled quantity triggers position limit rejections (`cannot_close_more_than_existing_position`).
- Auditing the broker's held position (`get_option_position_quantity()`) before placing exit orders guarantees that the exit quantity matches reality.
- Cancelling the remaining stop immediately upon partial fill and replacing with a limit order at Market Mid fills the remaining contract without leaving it stranded.

### Validation
- [x] Unit tests in `tests/test_partial_fill_stops.py` verifying partial fill cancellation, market mid limit replacement, VWAP price blending, and position clamping (11/11 passed).
- [x] Regression tests in `tests/test_v3_exchange_stop.py`, `tests/test_fill_time_breach_safety_phase4.py`, `tests/test_long_close_chase.py`, and `tests/test_close_fills.py`.

---

## 2026-10-19 — DRMA+H-M: Independent Sub-Strategy Paper / Live Execution

**Status:** implemented, verified & unit-tested  
**Tests:** `uv run pytest tests/test_drma_hm.py tests/test_drma_hm_dashboard.py` → 35 passed  
**Doc:** [design/2026-10-19_DRMA_HM_INDEPENDENT_SUBSTRATEGY_PAPER_LIVE.md](design/2026-10-19_DRMA_HM_INDEPENDENT_SUBSTRATEGY_PAPER_LIVE.md)

### What changed
- **Independent Sub-Strategy Config (`strategies/drma_hm/config.py`):**
  - Added tri-state configuration fields `dir_paper: Optional[bool] = None`, `non_dir_paper: Optional[bool] = None`, and `futures_paper: Optional[bool] = None` with properties falling back to global `paper` when `None`.
  - Added `has_any_live_sub_strategy` and `is_sub_strategy_paper(sub_strategy)` helpers.
- **Dynamic Multi-Broker Routing (`strategies/drma_hm/strategy.py`):**
  - Added dynamic broker selector `_get_broker(is_paper)` returning shared `JournalBroker` (paper) or `TastyTradeBroker` (live).
  - Updated `_enter_directional_trade()`, `_enter_non_directional_trade()`, and `_enter_directional_futures_trade()` to place orders via their respective sub-strategy broker and stamp `sub_strategy` and `paper` fields on active trade JSONs.
  - Updated `_exit_trade()` to close positions through the broker corresponding to `trade.get('paper')`.
- **System Framework & Safety Integration:**
  - `common/broker_factory.py`: `is_strategy_paper()` resolves sub-strategy names (`DRMA_HM_DIR`, `DRMA_HM_NONDIR`, `DRMA_HM_FUT`) via daily overrides and `strategies.yaml`.
  - `common/standalone_runner.py`: `_assert_paper_safety()` bypasses halt when `has_any_live_sub_strategy` is True, allowing mixed-mode operation.
  - `common/manual_close.py`: `close_structure()` prioritizes `trade.get('paper')` so manual closes honor actual execution mode.
- **Dashboard API & UI (`dashboard/server.py`, `dashboard/templates/index.html`):**
  - Updated `/api/drma_hm/today` to return `dir_paper`, `non_dir_paper`, and `futures_paper`.
  - Supported sub-strategy control endpoints `POST /api/strategy/<name>/control` and daily overrides.
  - UI Header Alert: Simplified top banner to clean Master Strategy status and toggle.
  - UI Parameters & Overrides Card: Relocated individual sub-strategy toggle buttons directly into each sub-strategy column header (`Non-Directional Spreads`, `Directional Spreads`, `Directional Futures`) with interactive toggle badges, hover micro-animations, and live confirmation modals.
  - Form Synchronization: Updated `drmaHmToggleSubPaper()` and `drmaHmSaveOverrides()` to sync dropdown select values and delete override keys when reverted to "Inherit Strategy Default".
  - Dynamic Override Hot-Reload: Updated `common/daily_overrides.py` and `strategies/base.py` to auto-reload modules and explicitly allow `dir_paper`, `non_dir_paper`, and `futures_paper` overrides, resolving `unknown override keys: ['non_dir_paper']` on running servers.

### Findings / Learnings
- Decoupling execution mode at the sub-strategy level eliminates the risk of testing options strategies live while allowing live execution of high-conviction futures scalps.
- Placing the toggle buttons directly inside each sub-strategy column header creates a far more cohesive UI than crowding the top alert banner.
- Adding dynamic module reload and explicit schema allowances in `set_overrides` avoids stale class definitions when background processes run continuously across days.

### Validation
- [x] Unit tests in `tests/test_drma_hm.py` verifying config inheritance, mixed-mode routing, and runner safety assertions.
- [x] Dashboard API tests in `tests/test_drma_hm_dashboard.py` verifying control endpoints, overrides, and `/today` payload.
- [x] Live HTTP endpoint validation (`GET /api/drma_hm/today`, `POST /api/strategy/DRMA_HM_SPX/overrides`, `DELETE /api/strategy/DRMA_HM_SPX/overrides/*`).
- [x] Regression tests in `tests/test_broker_factory_multi_strategy.py` and `tests/test_standalone_runner.py`.

---

## 2026-10-03 — Dale Perryman NFH 0DTE Iron Fly Scaled to XSP (Mini-SPX) Implementation

**Status:** implemented, verified & unit-tested  
**Tests:** `.venv\Scripts\python.exe -m pytest tests/test_iron_fly_xsp_*.py` → 21 passed (70 passed across all strategy & integration tests)  
**Doc:** [design/2026-10-03_IRON_FLY_XSP_SCALED_STRATEGY_IMPLEMENTATION.md](design/2026-10-03_IRON_FLY_XSP_SCALED_STRATEGY_IMPLEMENTATION.md)

### What changed
- **Synthetic 2-Fly Center Mechanism (`strategies/iron_fly_xsp/rules.py`):**
  - Resolved XSP integer strike constraint (no 0.5 strikes): SPX centers ending in 5 (e.g. 7755 -> 775.5) execute as a paired set of 2 flies: 1 fly at 775 and 1 fly at 776 ($\text{avg} = 775.50$). SPX centers ending in 0 (e.g. 7750 -> 775.0) execute 2 flies at 775.
  - Exactly 2 XSP contracts executed per fly trigger (0.2x SPX notional).
  - Wing widths scale down by 10 to integer strikes (e.g. 30 pt SPX -> 3 pt XSP) with clash prevention against short strikes in the book.
  - Break-even cut buffer scaled to 0.20 pt (from 2.0 pt in SPX).
- **Orders & Combos (`strategies/iron_fly_xsp/orders.py`):**
  - 4-leg combos using `.XSP{yymmdd}{C|P}{strike}` OCC symbols and combo tick rounding.
- **Paired State Book (`strategies/iron_fly_xsp/book.py`):**
  - `IronFlyXspPair` tracks pair metrics (`credit_total`, `credit_avg`, `be_low`, `be_high`, P&L, fees, open risk).
  - `IronFlyXspBook` provides atomic JSON persistence and `structures` compatibility for manual closes.
- **Strategy Orchestrator (`strategies/iron_fly_xsp/strategy.py`):**
  - Inherits from `BaseStrategy(ABC)`. Dual 15s main sweep and 2s background spot cut loop.
  - Auto-derives XSP spot as $SPX / 10.0$ when direct `.XSP` streamer quote is absent.
  - Scaled safety caps ($3,000 max open risk, $500 daily loss stop, 20 max pairs/day).
- **Dashboard & API Integration (`dashboard/`):**
  - Top P&L stat card, dedicated "Iron Fly XSP" navigation tab, active paired ladder table with distance-to-BE indicators, pending approvals, and activity diary.
  - REST endpoints: `/api/iron_fly_xsp/today`, `/api/iron_fly_xsp/approve`, `/api/iron_fly_xsp/skip`, and `/api/iron_fly_xsp/close_all`.

---

## 2026-10-03 — Dale Perryman NFH SPX 0DTE Iron Fly Strategy Implementation

**Status:** implemented, verified & unit-tested  
**Tests:** `.venv\Scripts\python.exe -m pytest tests/test_iron_fly_*.py` → 33 passed  
**Doc:** [design/2026-10-03_IRON_FLY_SPX_0DTE_STRATEGY_IMPLEMENTATION.md](design/2026-10-03_IRON_FLY_SPX_0DTE_STRATEGY_IMPLEMENTATION.md)

### What changed
- **Pure Rules Engine** (`strategies/iron_fly/rules.py`):
  - Implemented complete Dale Perryman NFH rule set: calendar skip filters (3rd Wednesday, last weekday, FOMC, holidays), 0DTE ATM Straddle 83% decay trigger, 5 ratcheting decay tiers (triggers 2.5–25 pt, spacing 5–30 pt), ~$0.40 wing target width picker, §2.6a strike clash prevention (never buy what book is short, never center where book is long), 2-point tolerance break-even cuts, and cash settlement intrinsic calculation.
- **Orders & Combo Protocol** (`strategies/iron_fly/orders.py`):
  - Built 4-leg Net Credit entry combos and Net Debit exit combos with combo tick rounding (down for credits, up for debits to nearest 0.05).
  - Added broker order status normalization matching repo broker models.
- **State Book & Diary** (`strategies/iron_fly/book.py`):
  - Created `IronFlyBook` and `IronFly` models tracking full butterfly lifecycle (`OPENING`, `OPEN`, `CLOSING`, `CLOSED`, `EXPIRED`, `FAILED`).
  - Dual schema compatibility: serializes both `flies` (dict) and `structures` (list) for seamless `common/manual_close.py` operations.
  - Comprehensive diary metrics: `em_open`, `em_now`, `ratio_vs_83`, `current_tier`, `spot_trail`, and event log.
- **Executors & Chase Loops** (`strategies/iron_fly/executor.py`):
  - `PaperExecutor`: simulates live quotes, natural debit fills, and $1.30/leg/contract fee accounting.
  - `BrokerExecutor`: implements MEIC cancel-and-re-quote chase loop (5s wait, cancel, verify cancel, re-quote with -0.05 step down to floor, max 6 attempts / 45s) and cut chase loop.
- **Strategy Orchestrator** (`strategies/iron_fly/strategy.py`):
  - Built `IronFlyStrategy(BaseStrategy)` with dual cadences: 15s main sweep loop + 2s background break-even spot cut monitor.
  - Streamer symbol auto-subscription, risk caps enforcement ($15,000 max open risk, $2,500 daily loss stop, 20 max flies/day), and live approval modes.
- **Framework & Dashboard Integration**:
  - Registered `iron_fly_break_even_cut` stop profile in `blocks/stop/profiles/`.
  - Registered `STRATEGY_IRON_FLY = 'IRON_FLY_SPX'` in `common/trades_layout.py` and `common/standalone_runner.py`.
  - Added strategy configuration in `config/strategies.yaml` (`paper: true`, `confirmed: true`).
  - Added REST API routes `/api/iron_fly/today`, `/api/iron_fly/approve`, `/api/iron_fly/skip`, `/api/iron_fly/close_all` in `dashboard/server.py`.
  - Integrated Iron Fly in dashboard (`dashboard/templates/index.html`): top P&L stat card, nav tab item, active ladder table with live distance-to-BE, pending approval table, closed history, and activity diary.
- **Automated Verification** (`tests/test_iron_fly_*.py`):
  - Added 33 unit tests covering rules, order builders, book lifecycle, strategy runner, and dashboard endpoints (100% pass rate).

---

## 2026-10-01 — Vertical Spread Scale-Out (Partial Position Close) & Stop Sizing Safety

**Status:** implemented, live-verified & unit-tested  
**Tests:** `.venv\Scripts\pytest tests/test_scale_out.py` → 5 passed  
**Doc:** [design/2026-10-01_SPREAD_SCALE_OUT_FEATURE.md](design/2026-10-01_SPREAD_SCALE_OUT_FEATURE.md)

### What changed
- **Scale-Out Core Engine** (`common/scale_out.py`):
  - Added `scale_out_spread(broker, prices, filename, close_qty, debit_limit)` executing protected 4-step sequence: validates $1 \le \text{qty} < \text{holding}$, safely cancels active broker stop on short leg, places spread close order for `close_qty`, places replacement stop for remaining `holding - close_qty` contracts at same trigger price, and logs full audit trail.
  - Fail-safe stop restoration: if the spread close order is rejected by broker, immediately restores stop for original full holding.
- **Stop Monitor & Fill Sync Safety** (`blocks/stop/fill_sync.py`, `blocks/stop/monitor.py`, `blocks/stop/v3/trade_slot.py`):
  - Changed `stop_is_current()` to check exact quantity match `stop_quantity == stop_qty_for_state(state)`.
  - In `monitor.py`: stop resize check changed from `< filled` to `!= filled` to downward resize if needed.
  - In `trade_slot.py` and `monitor.py`: added `'scale_outs'`, `'initial_quantity'`, and `'stop_quantity'` to disk merge policies.
  - Software breach closes (`replace_with_spread_close`, `replace_with_limit_close`) use `stop_qty_for_state(state)` (remaining contracts), guaranteeing closed contracts are never touched.
- **P&L Realization & Dashboard Integration** (`dashboard/server.py`, `dashboard/manual_spread_handlers.py`):
  - Updated `_trade_pnl` to accumulate realized P&L from `trade['scale_outs']` on both live and closed trades.
  - Added API endpoints `/api/manual_spread/scale_out` and `/api/scale_out`.
- **Dashboard UI Enhancements** (`dashboard/templates/index.html`):
  - Added `#modal-scale-out` modal with contracts input, live remaining preview, mid-spread limit override, and safety callout.
  - Added "Scale Out" button in Active Manual Spreads card header (`msScaleOutSelected()`).
  - Added "Scale" action button in `ms-active-tbody` Action column when `r.filled > 1` and open (`openScaleOutModal(fn)`).
  - Added scaled quantity badge in Qty column (`s.quantity -s.scaled_qty`).

### Validation
- [x] 5/5 passed in `tests/test_scale_out.py`.
- [x] 14/14 passed across `tests/test_trade_pnl_qty.py`, `tests/test_build_manual_trades.py`, `tests/test_fill_sync.py`.
- [x] 6/6 passed in `tests/test_v3_manual_kill.py`.
- [x] Dashboard running live on port 5002 with live `/api/manual_spread/scale_out` and `/api/scale_out` routes.

---

## 2026-09-09 — Exchange Stop Placement Priority & Streamer Reconnection Resilience

**Status:** implemented, live-verified & unit-tested  
**Tests:** `.venv\Scripts\pytest tests/test_v3_exchange_stop.py` → 3 passed  
**Doc:** [fixes/2026-09-09_EXCHANGE_STOP_PRIORITY_AND_STREAMER_RESILIENCE.md](fixes/2026-09-09_EXCHANGE_STOP_PRIORITY_AND_STREAMER_RESILIENCE.md)

### What changed
- **Exchange Stop Priority over Quote Staleness** (`blocks/stop/v3/supervisor.py`):
  - Moved `_requires_exchange_stop` and `_ensure_stop_for_filled_qty()` ahead of `mqtt_cache_stale` and `streamer_stale` checks in `_scan_open_slot()`.
  - Fixes an operational bug where a streamer crash caused MQTT cache staleness, which triggered an early return and completely prevented new filled positions (`11-45_C`, `11-45_P`, `12-15_P`) from placing their exchange stops at TastyTrade. Broker stop orders are now placed immediately regardless of local streamer staleness.
- **Streamer Disconnect Recovery Loop** (`streaming/publish_tastytrade.py`):
  - Wrapped `main()` in an automated reconnection loop with exception logging to recover from websocket disconnects or DXLink transport interruptions after a 3s backoff instead of terminating the process with status 1.
- **Unit Testing** (`tests/test_v3_exchange_stop.py`):
  - Added `TestV3ExchangeStopPriorityWhenStale` testing that open slots without exchange stops successfully invoke stop placement even when `prices.is_stale()` is True.

### Validation
- [x] Streamer restarted and live (`trades/streamer_health.json` updated with live SPX stream).
- [x] All 3 missing stop orders submitted and working at TastyTrade (`11-45_C` #8247501, `11-45_P` #8247503, `12-15_P` #8247506).
- [x] 3/3 passed in `tests/test_v3_exchange_stop.py`.

---

## 2026-09-03 — MEIC Companion Breach Safety, Dynamic Spread Width Expansion & Dashboard Phase Tooltip

**Status:** implemented & unit-tested  
**Tests:** `.venv\Scripts\python -m unittest tests/test_meic_oco_and_breakeven.py tests/test_dashboard_phase_display.py tests/test_strike_guard.py tests/test_spread_scan_pick.py` → 35 passed  
**Doc:** [fixes/2026-09-03_MEIC_COMPANION_BREACH_DYNAMIC_WIDTH_AND_PHASE_TOOLTIP.md](fixes/2026-09-03_MEIC_COMPANION_BREACH_DYNAMIC_WIDTH_AND_PHASE_TOOLTIP.md)

### What changed
- **Companion Breach Protection & Strict Date Scoping** (`blocks/stop/companion_breach.py`):
  - `find_companion_trade_state()` strictly scopes candidate files to today's date using `_extract_trade_date()`, completely ignoring historical files from past days (e.g. August 24 files on September 3).
  - `is_companion_breached()` returns `False` if companion status is `pending_fill`, `failed`, or `cancelled` with zero short fills (`filled_quantity <= 0`), preventing premature breakeven stop-outs on surviving spreads (e.g. 1:30 CCS).
- **Ghost Trade Cleanup & Strike Guard Defense** (`blocks/entry/meic_worker.py`, `common/strike_guard.py`):
  - Unfilled / failed entry attempts now transition trade state to `'cancelled'` rather than leaving lingering `pending_fill` states in `trades/active/`.
  - `leg_overlap_conflict()` ignores `pending_fill` trades whose broker order is cancelled, rejected, or has 0 fills.
- **Dynamic Spread Width Expansion on Clashes (25 → 30 or 35)** (`blocks/entry/spread_scan.py`):
  - In `_resolve_overlap_candidate()`, when a candidate spread encounters a leg clash, it tests expanding spread width (25 → 30 → 35 up to `spread_width_max`) while keeping the short strike before falling back to strike shifting.
  - Initialized `wmax` in `scan_credit_spreads()` to `spread_width_max` (or default 35) when a single `spread_width` is specified, giving width expansion headroom.
- **Dashboard Phase Column Badges & Chronological Hover Tooltip** (`dashboard/server.py`, `dashboard/templates/index.html`):
  - `_phase_display()` now renders explicit terminal outcome badges: `Closed (PT 15¢)`, `Closed (Stop)`, `Settled (Expiry)`, `Closed (Breach)`, and `Breakeven (1×)`.
  - `_format_phase_tooltip()` generates a multi-line, timestamped progression of stop actions and price adjustments from `stop_history`.
  - Added dotted underline styling (`cursor: help; text-decoration: underline dotted 1px;`) and tooltip rendering in `index.html`.

### Validation
- [x] 35/35 passed across `tests/test_meic_oco_and_breakeven.py`, `tests/test_dashboard_phase_display.py`, `tests/test_strike_guard.py`, and `tests/test_spread_scan_pick.py`.

---

## 2026-09-02 — MEIC OCO Sibling Order Resolution, Ghost BTO Prevention & Position Pre-Checks

**Status:** implemented & unit-tested  
**Tests:** `.venv\Scripts\python -m unittest tests/test_meic_oco_and_breakeven.py` → 12 passed  
**Doc:** [fixes/2026-09-02_MEIC_OCO_SIBLING_RESOLUTION_AND_GHOST_BTO_PREVENTION.md](fixes/2026-09-02_MEIC_OCO_SIBLING_RESOLUTION_AND_GHOST_BTO_PREVENTION.md)

### What changed
- **TastyTrade Complex Order Sibling Aggregation** (`brokers/tastytrade_broker.py`):
  - In `get_order_status()`, querying `live_orders` by `complex_order_id` aggregates all sibling child orders:
    - If any sibling is `filled`, returns `status = 'filled'` with the actual execution fill price.
    - If any sibling is `working` / `live`, returns `status = 'working'`.
    - If all siblings are `cancelled` / `expired`, returns `status = 'cancelled'`.
  - Prevents the cancelled stop leg of an OCO from masking the filled 15¢ profit target leg.
- **Short Position Pre-Checks in Stop Placement & Breakeven** (`blocks/stop/monitor.py`):
  - Queries `broker.get_single_leg_position_qty(short_sym)` before placing stops in `_place_short_stop()` or adjusting stops in `adjust_stop_to_breakeven()`.
  - If held short quantity is $\le 0$ (already flat or long), skips order submission, transitions state to `'closing'`, and sets `close_mechanism = 'broker_position_flat'`. Zero buy orders are submitted, eliminating ghost BTO debit leg loops and margin exhaustion.
  - Clamps stop order size to `min(desired_qty, abs(held_qty))`.
- **Fast Stop Sync for OCO** (`blocks/stop/monitor.py`):
  - Included `'OCO'` in `_sync_working_stop_order()` type check alongside `'STOP_LIMIT'` and `'LIMIT'`.

### Validation
- [x] 12/12 passed in `tests/test_meic_oco_and_breakeven.py`.

---

## 2026-08-21 — MEIC Expired Spreads Table View & Day P&L Settlement Fix

**Status:** implemented & unit-tested  
**Tests:** `uv run pytest tests/test_dashboard_meic_pnl.py tests/test_phase6_dashboard_runtime_status.py tests/test_manual_dashboard_trades.py` → 14 passed  
**Doc:** [fixes/2026-08-21_MEIC_EXPIRED_SPREADS_DASHBOARD_PNL_FIX.md](fixes/2026-08-21_MEIC_EXPIRED_SPREADS_DASHBOARD_PNL_FIX.md)

### What changed
- **Expired Spread Inclusion in Dashboard P&L** (`dashboard/server.py`):
  - Added `'expired'` to `MEIC_GRID_PNL_STATES`.
  - `_counts_toward_meic_day_pnl()` and `build_summary()` now count all 0DTE expired spreads (+100% full profit) toward Day P&L and closed counts.
- **Tranche Grid Table Display** (`dashboard/templates/index.html`):
  - Added `'expired'` to `spreadRowHasData(s)` in frontend JS so expired trades retain full visible entry credits, $0.00 exit prices, and profit numbers instead of being rendered as empty dashes (`–`).
- **History Tab Live Money Filter Default & PermaBull Integration** (`dashboard/server.py`, `dashboard/db.py`, `dashboard/templates/index.html`):
  - Fixed History tab issue where default `All $` blended Live money with Paper Skyline (-$780 paper loss at expiry) yielding `-$230`.
  - History tab now defaults to `Live $` (`money=live`), correctly displaying the live `+$550.00` total (+ $430 MEIC, + $120 Perma Bull).
  - Added full support for `STRATEGY_PERMA_BULL` across database queries, breakdown helpers, and frontend dropdowns/badges.
- **Trade History Table High-Contrast Dark Mode** (`dashboard/templates/index.html`):
  - Added `.history-table` styling with `#181c28` headers, `#1a1e2b`/`#202536` alternating row backgrounds, `#f1f5f9` cell text, bright short/long strike colors, and vibrant badges.

### Validation
- [x] 6/6 passed in `tests/test_dashboard_meic_pnl.py`
- [x] 8/8 passed in `tests/test_phase6_dashboard_runtime_status.py` and `tests/test_manual_dashboard_trades.py`
- [x] 59/59 passed across full test suite
- [x] Verified `/api/daily_calendar?days=30&money=live` returns `pnl: +550.00` (MEIC: +430.00, Perma Bull: +120.00)

---

## 2026-08-21 — MEIC Tranche Probe Unpause Gating Fix & PermaBull Dashboard High Contrast

**Status:** implemented & unit-tested  
**Tests:** `uv run pytest tests/test_probe_coordinator.py tests/test_entry_runner.py` → 15 passed  
**Doc:** [fixes/2026-08-21_MEIC_TRANCHE_PROBE_UNPAUSE_AND_DASHBOARD_CONTRAST.md](fixes/2026-08-21_MEIC_TRANCHE_PROBE_UNPAUSE_AND_DASHBOARD_CONTRAST.md)

### What changed
- **MEIC Probe Unpause Gating Fix** (`common/probe_coordinator.py`, `blocks/entry/runner.py`):
  - Fixed issue where `10-45` tranche was blocked at entry window with `rest_probe_missing` after being unpaused on the dashboard.
  - `meic_tranches_from_session_plan()` now includes paused slots by default so the probe coordinator pre-probes all configured slots at $T-30\text{s}$.
  - Added `ProbeCoordinator.schedule_tranche_probe(strategy, tranche_id)` for on-demand probe triggering.
  - `EntryMonitorRunner._gate_allows_spawn()` now automatically dispatches on-demand probes if a scheduled MEIC tranche is unpaused late or lacks a fresh probe record.
- **PermaBull Dashboard High Contrast** (`dashboard/templates/index.html`):
  - Added high-contrast dark theme styling for `#strategy-perma-bull`: `#1e293b` alert background with `#38bdf8` border, bold `#f8fafc` status values, and `#cbd5e1` configuration text.
  - Added `.perma-bull-table` with `#181c28` headers, `#1a1e2b`/`#202536` alternating row backgrounds, `#f1f5f9` cell text, and vibrant status badges.
- **Mandatory Documentation Protocol**:
  - Established `.cursor/rules/documenting-changes-and-fixes.mdc` and `.agents/rules/DOCUMENTATION_PROCESS.md` to enforce dated design/fix docs in `changes/fixes/` and living changelog updates for all future work.

### Findings / learnings
- Pre-entry REST probes should be scheduled for all planned tranches regardless of their initial pause status, ensuring that an operator unpausing a tranche dynamically is never blocked by a missing pre-tranche probe.

### Validation
- [x] 10/10 passed in `tests/test_probe_coordinator.py`
- [x] 5/5 passed in `tests/test_entry_runner.py`

---

## 2026-08-20 — MEIC Long Chase Double-Sell Prevention, Exponential Backoff & CCS Overlap Resilience

**Status:** implemented & unit-tested  
**Tests:** `uv run pytest tests/test_long_close_chase.py tests/test_strike_guard.py tests/test_spread_scan_pick.py` → 30 passed; full stop/chase/v3 suite → 272 passed  
**Doc:** [fixes/2026-08-20_MEIC_LONG_CHASE_DOUBLE_SELL_AND_CCS_OVERLAP_FIX.md](fixes/2026-08-20_MEIC_LONG_CHASE_DOUBLE_SELL_AND_CCS_OVERLAP_FIX.md)

### What changed
- **Double-Sell & Naked Short Prevention** (`blocks/stop/monitor.py`):
  - Strict Cancel-Before-Replace: when repricing/escalating a working long close order, sends cancel and confirms cancellation on next tick before submitting new limit/market order.
  - Race Condition Fill: if the previous order filled before cancel finished at the exchange, auto-records fill price and finalizes closed without sending secondary orders.
  - Position Pre-Check: `get_single_leg_position_qty()` checks live broker net long position before submitting `SELL_TO_CLOSE`. If $\le 0$ contracts held, skips order submission and auto-finalizes close.
  - Fixed attempt 10 dual order placement bug (limit + market on same tick).
- **Exponential Backoff & Fail Tracking** (`blocks/stop/monitor.py`, `blocks/stop/v3/supervisor.py`):
  - Tracks `long_close_fail_count`, `long_close_last_error`, and `long_close_next_retry_at` with exponential backoff (5s to 60s max).
  - Stop supervisor `_poll_closing()` and monitor `_chase_long_close()` gate on cooldown.
- **Multi-Width Scan & Bi-Directional Overlap Shift** (`blocks/entry/spread_scan.py`, `common/strike_guard.py`):
  - Structured scan loops width-by-width so reaching `< credit_min` on one width does not prematurely abort evaluating wider spreads (e.g. 30, 35).
  - Bi-directional shift: if shifting CCS toward ATM exceeds `credit_max`, automatically tests reverse shift further OTM.
  - Added duplicate vertical spread conflict detection to prevent duplicate identical spreads across tranches.

### Findings / learnings
- In options trading with REST APIs, cancelling an order at the exchange is asynchronous. Never place a replacement order on the same execution tick as a cancel request; wait for the cancel confirmation report or query the net held position to guarantee no naked shorts are created.

### Validation
- [x] 12/12 passed in `tests/test_long_close_chase.py`
- [x] 10/10 passed in `tests/test_strike_guard.py`
- [x] 8/8 passed in `tests/test_spread_scan_pick.py`
- [x] 272/272 passed in core stop, V3 supervisor, and entry suite

---

## 2026-07-31 — Reorganize `changes/` folder + strategy north-star rule

**Status:** implemented  
**Tests:** n/a (docs only)

### What changed
- Split flat `changes/` into four subfolders: `design/`, `strategies/`, `sessions/`, `fixes/`
- Added `changes/README.md` index; **north star** is `changes/design/STRATEGY_DESIGN_PATTERN.md`
- Added `.cursor/rules/strategy-design-north-star.mdc` so agents read the north-star doc before strategy work
- Updated `V2_README.md`, changelog rule, and a few code comments pointing at old paths

### Findings / learnings
- Historical session/fix docs keep old inline `changes/FOO.md` links — use `changes/README.md` or folder names when navigating

### Validation
- [x] All 57 docs moved; root retains only `CHANGES_SINCE_2026-07-06.md` + `README.md`

---


After any meaningful change (code merge, live session finding, incident, config decision):

1. **Add a new `## YYYY-MM-DD` section above the previous entry** (newest first).
2. Include:
   - **What changed** — files, behavior, env vars
   - **Why** — incident, review finding, or goal
   - **Findings / learnings** — anything useful going forward
   - **Tests / validation** — pytest count, live smoke, checklist items
   - **Deferred** — what was explicitly not done
3. Link to deeper specs in `changes/` when they exist (incident docs, design docs).
4. Update the **Open items** table at the bottom if status changed.

### Entry template (copy for next change)

```markdown
## YYYY-MM-DD — Short title

**Status:** implemented | in progress | deferred | live-validated  
**Tests:** `uv run pytest tests/ -q` → N passed

### What changed
- ...

### Findings / learnings
- ...

### Validation
- [ ] ...

### Related
- [link](path)
```

---

## 2026-07-09 — Expanded market watch + sidecar SPX ladder (implemented)

**Status:** implemented  
**Tests:** `uv run pytest tests/test_market_data.py tests/test_spx_ladder.py tests/test_mqtt_prices_resilience.py -q` → **30 passed**

**Spec:** [MARKET_DATA_EXPANDED_WATCH_DESIGN.md](MARKET_DATA_EXPANDED_WATCH_DESIGN.md)

### What changed

| Area | Files | Behavior |
|------|-------|----------|
| **Watch universe** | `common/market_watch.py`, `market_data/config.py`, `market_data/watch_symbols.py` | `SPX, VIX, VXN, QQQ, IWM, TLT, GLD` — single source of truth |
| **DXLink / streamer** | `streaming/publish_tastytrade.py`, `streaming/ladder_subscribe.py` | `$VIX`/`$VXN` Quote; Trade → `__TSIZE` / `__VOL` aux topics; ladder union with cap + quarantine |
| **OHLCV** | `market_data/aggregator.py`, `market_data/indicators.py`, `common/mqtt_prices.py` | Non-SPX bars get `volume` column (sum `Trade.size`); SPX stays OHLC |
| **Sidecar ladder** | `market_data/spx_ladder.py`, `market_data/spx_ladder_snapshots.py`, `common/stream_ladder_symbols.py` | 200-symbol 0DTE grid → `streaming/spx_ladder_symbols.json` + `spx_ladder_quotes.csv` |
| **Trade legs unchanged** | `options_quotes.csv`, `optsymbols.json`, `blocks/stop/*` | Still 3 columns; MEIC/Manual registration untouched |
| **Health** | `common/streamer_health.py` | `ladder_enabled`, `ladder_symbol_count`, `total_subscribed_symbols`, `ladder_last_update`, `ladder_last_error` |

### Sidecar option collection kill switch

**Default: ON.** Disable only for the session:

```powershell
$env:MEIC_SIDE_OPTION_COLLECTION="0"   # OFF — no ladder JSON, no ladder subscribe, no ladder CSV
$env:MEIC_SIDE_OPTION_COLLECTION="1"   # ON  (explicit; same as unset)
```

When OFF, market_data logs once: `sidecar_option_collection_disabled`.  
**Watch symbols (VIX/VXN/QQQ/IWM/TLT/GLD OHLCV) keep recording** — only the SPX ladder sidecar stops.

Config defaults (`common/market_watch.py`):

```python
MARKET_DATA_OPTION_COLLECTION_ENABLED = True
SPX_LADDER_ENABLED = True
SPX_LADDER_QUOTES_ENABLED = True
SPX_LADDER_VOLUME_ENABLED = False   # ladder Quote-only by default
SPX_LADDER_MAX_ACTIVE_SYMBOLS = 500
SPX_LADDER_REFRESH_SEC = 60
```

### Live validation

**Ladder ON (default)** — after `run.py` restart during session:

- [ ] `VIX_1m.csv`, `VXN_1m.csv`, `TLT_1m.csv`, `GLD_1m.csv` growing
- [ ] QQQ/IWM/TLT/GLD/VIX/VXN bars have `volume` column; SPX does not
- [ ] `streaming/spx_ladder_symbols.json` updates ~60s; ~200 symbols
- [ ] `data/YYYY-MM-DD/spx_ladder_quotes.csv` growing (5 columns: mid only)
- [ ] `options_quotes.csv` still 3 columns (trade legs only)
- [ ] MEIC entry + stop monitor MQTT unchanged

**Ladder OFF** — set `MEIC_SIDE_OPTION_COLLECTION=0`, restart:

- [ ] Log line `sidecar_option_collection_disabled`
- [ ] No `spx_ladder_symbols.json` updates / no `spx_ladder_quotes.csv`
- [ ] Trade legs still stream; watch OHLCV still records

---

## 2026-07-08 (EOD) — Test trade isolation + History totals fix

**Status:** implemented; pushed to `origin/master`  
**Tests:** `uv run pytest tests/test_test_trades_excluded.py tests/test_build_manual_trades.py tests/test_expiry_settlement.py tests/test_history_stale_purge.py -q` → **21 passed**

### What changed

| Area | Files | Behavior |
|------|-------|----------|
| **Folder** | `trades/test/active/`, `trades/test/history/`, `trades/test/README.md` | Dedicated home for fixtures; excluded from History totals |
| **Detection** | `common/test_trades.py` | `is_test_trade()` — path under `trades/test/` or `trades/sandbox/`, known lots (`ms-99`, `ms-100`, `ms-v3`, …), `*_test.json` filenames |
| **History sync** | `dashboard/history_sync.py` | Skips test trades; `purge_stale_history_rows()` drops ghost SQLite rows on sync |
| **Dashboard** | `manual_spread/entry.py` | `load_dashboard_manual_trades()` skips test lots |
| **Scanners** | `common/expiry_settlement.py` | `iter_trade_json_paths()` / `iter_all_trade_json_paths()` skip `trades/test/` |
| **Filled qty** | `common/expiry_settlement.py`, `dashboard/db.py` | `effective_filled_quantity()` — `filled_quantity=0` means unfilled; skip cancelled/unfilled in `trade_to_history_row()` |
| **SQLite purge** | `dashboard/db.py` | `delete_trades_by_lots()`, `purge_known_test_trades_from_db()` |
| **Seed / cleanup** | `scripts/seed_dual_manual_kill_fixture.py`, `scripts/cleanup_test_history_artifacts.py` | Default fixture output → `trades/test/`; script moves stray test JSON out of production history |

### Why — History tab showed $1085 instead of $530

| Issue | Amount | Fix |
|-------|--------|-----|
| Test fixtures `ms-99`, `ms-100`, `ms-v3`, `ms1_P_test` in production `trades/history/` and SQLite | **$405** | Moved 11 files → `trades/test/history/`; `purge_known_test_trades_from_db()` |
| `ms-205` cancelled order (`filled_quantity=0`) settled as qty **5** due to `filled_quantity or quantity` bug | **$150** | `effective_filled_quantity()` + skip cancelled/unfilled |
| Real trades (4 manual + 7 MEIC legs) | **$530** | ✓ matches brokerage |

**After cleanup:** `daily_summary` for 2026-07-08 = **$530** (11 trades).

### Operator

```powershell
uv run python scripts/cleanup_test_history_artifacts.py   # re-run if test JSON lands in production history again
uv run python -c "from dashboard.db import purge_known_test_trades_from_db; print(purge_known_test_trades_from_db())"
```

Put future fixtures under `trades/test/` (not production `trades/history/`). **Restart dashboard** so live `/api/history` sync uses the `filled_quantity` fix.

### Validation

- [x] pytest test_trades + history_stale_purge + expiry_settlement + build_manual_trades
- [x] Local History tab → $530 after purge + sync
- [ ] Next session — confirm no test lots re-sync into SQLite after dashboard restart

---

## 2026-07-08 (late) — Index OHLC from MQTT ticks on arrival

**Status:** implemented  
**Tests:** `uv run pytest tests/test_market_data.py tests/test_mqtt_prices_resilience.py tests/test_option_snapshots.py -q` → **18 passed**

### What changed

| Area | Files | Behavior |
|------|-------|----------|
| **MQTT tick listeners** | `common/mqtt_prices.py` | `add_tick_listener` / `remove_tick_listener` — callback on every streamer publish |
| **Index tick → OHLC** | `market_data/recorder.py`, `market_data/watch_symbols.py`, `market_data/aggregator.py`, `market_data/mqtt_reader.py` | SPX/VIX/QQQ/VXN/IWM recorded **on each MQTT mid** (not 30s poll); queue drains on main loop |
| **Timestamps** | `meic0dte/app/utilities.py` | `central_from_epoch()` — tick time matches `central_now()` basis |
| **Raw tick log** | `market_data/config.py` | `*_polls.csv` is now one row per MQTT publish (filename unchanged) |
| **Options** | unchanged | `options_quotes.csv` still 3 min snapshot of spread legs — no OHLC |

### Why

- 30s polling produced **~2 samples/min** → wrong 1m/3m/5m high/low (missed intra-minute extremes).
- Stale MQTT cache during streamer outages compounded the problem (flat `7459.0` bars for hours on Jul 8 morning).

### Findings / learnings

- `samples` column in `SPX_1m.csv` is the quick health check: **1–2 = old poll path or stale feed**; **10+ = tick path with live streamer**.
- Restart `run.py` after deploy so `market_data` child picks up tick listeners; pre-restart bars in today's CSVs stay sparse.

### Validation

- [x] pytest market_data + mqtt tick listener + option snapshots
- [ ] Live session tomorrow — confirm `samples` >> 2 on new 1m bars after restart

### Related

- Option snapshots (unchanged): section **2026-07-07 (late) — Option quote snapshots** below

---

## 2026-07-08 — Stop monitor MQTT hardening (P0-C / P0-B / P0-A) + ms-187 incident

**Status:** implemented; live-validated after **13:17 CT** restart with new code  
**Tests:** `uv run pytest tests/ -q --ignore=tests/adhoc_integration.py` → **341 passed**

**Specs:** [STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md](STOP_MONITOR_MQTT_CACHE_FIX_PLAIN_ENGLISH.md), [LIVE_SESSION_2026-07-08.md](LIVE_SESSION_2026-07-08.md)

### What changed (4 commits — pushed to `origin/master`)

| Commit | Area | Summary |
|--------|------|---------|
| P0-C | `blocks/stop/v3/supervisor.py`, `blocks/stop/runner.py`, `common/service_health.py` | Atomic `heartbeat.json` write; launcher read retry; distinct absent vs unreadable vs stale messages |
| P0-B | `common/mqtt_prices.py`, `trades/mqtt_cache_health.json` | MQTT reconnect/backoff, 30s staleness gate, `cache_health()` file; launcher **STOP_MONITOR MQTT** alert |
| P0-A | `blocks/stop/v3/quotes.py`, `blocks/stop/monitor.py` | `resolve_leg_mid()` — MQTT → REST → block; **no silent $0.05** auto long close |
| Visibility | `blocks/stop/breach_watch.py`, `blocks/stop/v3/supervisor.py`, `run.py`, `dashboard/` | `stale_mqtt` breach gate; separate dashboard dots for StopMon heartbeat vs SM MQTT vs streamer |

### Incident — ms-187 (Jul 8 morning)

- Exchange stop filled @ $1.65; auto long STC @ **$0.05** never filled (frozen MQTT cache since ~08:45 streamer exit).
- Manual **Close selected** on ms-188 used broker REST correctly (`source=broker_rest`).
- Root cause: stop_monitor `MqttPriceCache` had no reconnect/staleness; long-close path had no REST fallback.

### Live validation (13:17+ CT restart with hardened code)

| Check | Result |
|-------|--------|
| `trades/heartbeat.json` | Fresh (v3, loop advancing) |
| `trades/mqtt_cache_health.json` | `connected: true`, `stale: false`, `age_seconds` &lt; 1s |
| `check_stop_monitor_health()` | **ok** |
| `check_mqtt_cache_health()` | **ok** |
| `check_streamer_health()` | **ok** |
| `scan_price_gate_trades()` | **ok** (no stuck `waiting_mqtt`) |
| Open trade `breach_watch` | `short_mqtt` / `long_mqtt` true, `mqtt_cache_stale: false`, `breach_arm_status: armed` |
| Stop monitor log | `MQTT price cache connected; subscribed TASTYTRADE/#` @ 13:17:20 |

**Note:** Terminal may still show pre-13:17 `heartbeat missing` lines from the **old** binary (before P0-C). After the 13:17 restart, health files and breach watch look correct; watch for **new** absent/unreadable messages (should be rare/absent).

### Operator cleanup (local)

- Removed test artifact `trades/history/MANUAL_SPREAD/ms-v3_C_test.json`
- Removed closed orphans still under `trades/active/` (`ms-188`, `ms-189`, `12-30_P`) — copies remain in `trades/history/`
- Purged **6** SQLite rows for known test lots (`ms-99`, `ms-100`, `ms-v3`, …) via `purge_known_test_trades_from_db()`

**History / test-trade code:** see section **2026-07-08 (EOD) — Test trade isolation + History totals fix** above.

### Deferred (unchanged)

- P2 single shared MQTT consumer
- P1 floor chase → MARKET as primary fix (superseded by no-$0.05 + REST ladder)

### Related files touched

```
common/mqtt_prices.py, common/service_health.py
blocks/stop/monitor.py, blocks/stop/breach_watch.py
blocks/stop/v3/quotes.py, blocks/stop/v3/supervisor.py
blocks/stop/runner.py, run.py
dashboard/server.py, dashboard/templates/index.html
tests/test_service_health.py, tests/test_mqtt_prices_resilience.py
tests/test_long_close_chase.py, tests/test_breach_watch.py, ...
```

---

## 2026-07-07 (late) — Option quote snapshots + quiet terminal logging

**Status:** implemented  
**Tests:** `uv run pytest tests/test_option_snapshots.py tests/test_logging_config.py tests/test_service_health.py -q` → **10 passed**

**Specs:** [TERMINAL_LOGGING_QUIET.md](TERMINAL_LOGGING_QUIET.md)

### What changed

| Area | Files | Behavior |
|------|-------|----------|
| **Option quote log** | `common/stream_option_symbols.py`, `market_data/option_snapshots.py`, `market_data/recorder.py`, `market_data/config.py` | Every **3 min**, all spread legs from `optsymbols.json` → `data/YYYY-MM-DD/options_quotes.csv` (`snapshot_ts,symbol,mid`). No OHLC. |
| **Quiet terminal** | `common/logging_config.py`, `common/service_health.py`, `run.py`, `blocks/stop/run.py`, `streaming/publish_tastytrade.py`, `dashboard/server.py` | Microservices log **file-only**. Terminal shows **WARNING+** only: subprocess death/restart, streamer SPX stale, stop_monitor heartbeat stale, dashboard died, skip-day reason. Silent when healthy. |
| **Child stdout** | `run.py` | Subprocesses use `stdout/stderr=DEVNULL` so child INFO does not leak to terminal. |

### Validation

- [x] pytest option snapshots + logging + health
- [x] Live session — `options_quotes.csv` grows after entries (Jul 8 ~12:12 CT once streamer healthy)
- [ ] Live session — terminal quiet; alert on `kill streamer`

---

## 2026-07-07 (late) — Tranche grid: SW Breach on closed rows, entry/exit times

**Status:** implemented  
**Tests:** `uv run pytest tests/test_breach_watch.py tests/test_trade_times.py -q` → **13 passed**

### What changed

| Area | Files | Behavior |
|------|-------|----------|
| **SW Breach column** | `blocks/stop/breach_watch.py`, `dashboard/templates/index.html` | Closed and closing rows now show persisted `breach_watch` snapshot (threshold / gap). Breach cell no longer gated on live-price mode — fixes blank SW Breach on software-breached 7485P rows after close. |
| **Exit / entry times** | `dashboard/trade_times.py`, `dashboard/server.py`, `dashboard/manual_spread_handlers.py`, `index.html` | **MEIC** tranche grid: **Exit Time** only (`close.timestamp`, else `short_closed_at` while closing). **Manual** active grid: **Entry Time** + **Exit Time** (`entry.timestamp` / close). Displayed as `HH:MM` CT. |
| **Tests** | `tests/test_breach_watch.py`, `tests/test_trade_times.py` | Closed breached snapshot, closing live path, time helpers. |

### Findings / learnings

- SW Breach is **not** a yes/no flag — format is **`threshold / gap`** where gap = threshold − spread mid; **negative gap** = past breach line.
- **State = Breached** comes from `close_mechanism` (`software_breach`); the SW Breach numbers may be a **stale snapshot** if `breach_watch` was saved before the final move (e.g. 12-00_P showing `+0.25` room while state is Breached).
- Operator asked for post-close breach visibility to reconcile Jul 7 afternoon breach wave without reading JSON/logs.

### Validation

- [x] pytest breach + trade_times suites
- [ ] Dashboard refresh — closed 7485P rows show SW Breach; Exit Time populated on breached MEIC rows

---

## 2026-07-07 (EOD) — Jul 7 live session: F-9 close, shared-stop P0, EOD settlement, breach review

**Status:** implemented (code); live-validated partial; new day = fresh JSONs (no repair needed on prior shared-stop state)  
**Tests:** `uv run pytest tests/test_shared_stop_per_tranche.py tests/test_inspect_spread_position.py tests/test_expiry_settlement.py tests/test_v3_*.py -q` → **49+ passed** (session-related suites)

**Operator log:** [LIVE_SESSION_2026-07-07.md](LIVE_SESSION_2026-07-07.md)  
**Shared-stop spec:** [SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md](SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md)

---

### Session timeline (CT)

| Time | Event | Outcome |
|------|-------|---------|
| 08:43–09:15 | **ms-184** manual put 7445/7400 — open, stop, exchange-stop exit | ✓ V3 path OK; did not exercise manual-close preflight |
| 09:16 | **ms-185** manual put 7425/7400 qty 6 — open + stop | ✓ |
| 11:04–11:07 | Streamer stale on ms-185 | Breach checks frozen (F-3); exchange stop still live |
| 11:17 | Dashboard **Close** on ms-185 | Stop cancelled; spread close **blocked** — `preflight_mismatch` |
| ~11:25 | **F-9 fix** shipped (`_signed_position_qty`) | Code fix; ms-185 already flat manually @ 11:30 |
| ~13:16 | **ms-186** manual dashboard close | ✓ Validated after F-9 restart |
| 11:59–13:44 | MEIC 7485P tranches (12-00_P, 12-30_P, 01-45_P) | **Shared stop** `481561791` reconciled into multiple JSONs — only 1 broker stop |
| ~13:49 | Software breach wave on 7485P | 12-30_P, 01-45_P closed via `software_breach`; large slippage; cross-tranche stop fights |
| 15:00+ | **02-00_C** EOD PnL wrong | Stale `trades/settlement/2026-07-07.json` SPX **7524.29** → showed −$374 vs +$55 at SPX ~7503 |
| EOD | MQTT settlement capture + source priority fix | SPX **7503.205** captured; 02-00_C → +$55 |
| Evening | **Shared stop per tranche (P0)** implemented | Production reconcile no longer adopts by symbol; repair CLI only |

---

### 1. Incident — ms-185 manual close blocked (F-9 preflight)

**Symptom:** Dashboard Close cancelled exchange stop `481416212` but never placed spread close (~450+ recovery polls, `exit_error=preflight_mismatch`).

**Root cause:** Tasty positions use **unsigned** `quantity` + `quantity_direction` (`Short` / `Long`). `inspect_spread_position()` expected signed qty (short &lt; 0) → `mismatch` on valid 6-lot vertical.

**Fix**

| File | Change |
|------|--------|
| `brokers/tastytrade_broker.py` | `_signed_position_qty()` — Short → negative, Long → positive before F-9 closable check |
| `tests/test_inspect_spread_position.py` | ms-185 7425/7400 scenario (8 tests) |

**Operator impact:** Naked short exposure after stop cancel until manual flat @ Tasty (~11:30, +$0.65/sp vs $0.70 entry).

**Validation:** ms-186 manual close ~13:16 after monitor restart — pass.

**Deferred:** Recovery backoff when `exit_error=preflight_mismatch` repeats (position API storm).

---

### 2. Incident — MEIC shared stops on same strike (7485P / 7535C)

**Symptom:** Second and third tranches on **7485P** (and calls on **7535C**) did not get their own exchange stops. JSON for 12-30_P, 01-45_P pointed at **12-00_P**’s stop `481561791`.

**Root cause**

| Mechanism | Location |
|-----------|----------|
| Slow sync calls `find_working_close_order(short_sym)` and **adopts** first BTC | `StopMonitor._reconcile_active_stop_with_broker()` |
| `stop_is_current()` true per JSON after adopt → `setup_initial_stop()` skipped | `fill_sync.py`, `_ensure_stop_for_filled_qty()` |

**Afternoon impact (~13:49 CT)**

- Software breach **did** close unprotected puts (resilience worked).
- Breach cancelled the **one** shared stop; tranches **competed** for limits (cancel 422 retries, reprice chase).
- **12-00_P** briefly had **no** exchange stop until replaced ~13:49:41.
- Operator slippage large (−$95 to −$105/lot vs 2× credit policy) — execution gap (short mid chase), not PnL math bug.

**Investigation — breach timing (deferred, no code change)**

| Lot | Breach threshold | Spread at fire | Breach → short fill | Breach → spread flat |
|-----|------------------|----------------|---------------------|----------------------|
| 01-45_P | $0.80 (2× $0.30 + $0.20) | $0.83 @ 13:49:21 | ~14s | ~46s (30s long-leg delay dominates) |
| 12-30_P | $1.10 (2× $0.45 + $0.20) | $1.30 @ 13:49:25 | ~8s | ~40s |

Threshold uses **`two_x_net_credit + $0.20`**, not raw 2× spread — fires later than operator “spread at 2×” mental model. Tightening deferred.

---

### 3. Fix — Shared stop per tranche (P0 live-safety, evening)

**Operator decisions (recorded in plain-English doc):**

- **Stop placement:** always one broker stop per tranche JSON — never adopt another tranche’s BTC during normal sync/fill.
- **Close / cancel:** within-trade only — breach/manual cancel **this JSON’s** `active_stop.order_id` only.
- **Repair:** explicit CLI only (`sync-broker-stop` / `repair-orphaned-stops`), dry-run default, strict qty/price/unclaimed matching; refuse ambiguous matches.

**What changed**

| Area | Files | Behavior |
|------|-------|----------|
| Per-tranche reconcile | `blocks/stop/monitor.py` | `_reconcile_active_stop_with_broker()` refreshes **own** `order_id` only; no `find_working_close_order` |
| Stop place | `monitor.py` `_ensure_stop_for_filled_qty()` | Places new stop even if another BTC exists on symbol |
| Ownership guard | `blocks/stop/stop_ownership.py` | V3 cycle scans duplicate `active_stop.order_id`; `CRITICAL` + `lifecycle.stop_ownership_conflict`; blocks auto-place until repair |
| `stop_is_current` | `blocks/stop/fill_sync.py` | `ownership_conflict=True` → not current |
| V3 supervisor | `blocks/stop/v3/supervisor.py` | Ownership scan each cycle; conflict-aware stop gate |
| Repair-only adopt | `blocks/stop/broker_sync.py` | `repair_orphan_stop()` — strict match; `adopt_active_stop_from_broker()` → repair wrapper |
| CLI | `tests/adhoc_integration.py`, `scripts/repair_orphaned_stops.py` | `sync-broker-stop` / `repair-orphaned-stops` — **`--apply`** to write JSON |
| Removed from production | `monitor.py` | Dead `_cancel_all_close_orders_on_short` path; `cancel_all_close_orders_on_short` = repair CLI only |

**Tests:** `tests/test_shared_stop_per_tranche.py` (12), updated `tests/test_broker_sync.py`, `tests/test_stop_fill_long_close.py`

**Next session:** Fresh JSONs — no need to repair Jul 7 shared-stop state. Restart stop monitor after deploy.

```powershell
# If ever needed on stale JSONs (dry-run first):
uv run python tests/adhoc_integration.py sync-broker-stop
uv run python tests/adhoc_integration.py sync-broker-stop --apply
```

---

### 4. Fix — EOD settlement SPX source priority + MQTT capture

**Symptom:** **02-00_C** (7520/7545 calls) showed **−$374** at EOD; SPX ~7503 → should be **+$55** (OTM, expire $0).

**Root cause:** `trades/settlement/2026-07-07.json` had stale manual **`spx_close: 7524.29`** (above 7520 short strike). Settlement path preferred that file over polls/MQTT.

**Fix**

| File | Change |
|------|--------|
| `common/expiry_settlement.py` | Priority after 15:00 CT: locked manual → **mqtt_settlement** → OHLC → polls; ignore unlocked stale manual if &gt;5pt disagree |
| `common/expiry_settlement.py` | `capture_mqtt_settlement_close()` → `data/YYYY-MM-DD/spx_mqtt_settlement.json` |
| `blocks/stop/v3/supervisor.py` | `_maybe_capture_mqtt_settlement()` each cycle after 3 PM |
| `dashboard/server.py` | `/api/history/sync` with `spx_close` writes `locked: true` operator settlement |

**Result:** Captured MQTT **7503.205** → 02-00_C **+$55**.

**Tests:** `tests/test_expiry_settlement.py` (10 passed)

---

### 5. Deferred / tabled (no code this session)

| Topic | Status | Notes |
|-------|--------|-------|
| Software breach threshold | **Deferred** | Change `two_x_net_credit + $0.20` → raw 2× spread? |
| Breach execution price | **Deferred** | Cap limit to ~2× spread debit vs short mid chase |
| Recovery backoff on `preflight_mismatch` | **Deferred** | ms-185 API storm |
| `TESTING.md` doc drift | **Deferred** | Still says `two_x_short + 0.20` |

---

### Jul 7 validation sign-off

| Check | Result | Notes |
|-------|--------|-------|
| Manual spread open + stop | **pass** | ms-184, ms-185, ms-186 |
| Manual dashboard close | **pass** (after F-9) | ms-185 fail → fix; ms-186 ~13:16 |
| Exchange stop exit | **pass** | ms-184 |
| MEIC per-tranche stops | **fail → fixed evening** | Shared-stop P0; fresh JSONs next day |
| Software breach resilience | **pass** | Closed puts when stops missing; slippage high |
| Software breach policy | **review** | Threshold offset + execution chase |
| EOD settlement PnL | **pass** (after fix) | MQTT capture + priority |
| Streamer / stop monitor | **partial** | Stale episodes; breach wave ~13:49 |

---

### Files touched (Jul 7 — all code changes)

```
brokers/tastytrade_broker.py          # F-9 _signed_position_qty
common/expiry_settlement.py           # EOD settlement priority + MQTT capture
blocks/stop/monitor.py                # Per-tranche reconcile; ownership quarantine
blocks/stop/broker_sync.py            # repair_orphan_stop; no production adopt
blocks/stop/stop_ownership.py         # NEW — duplicate order_id guard
blocks/stop/fill_sync.py              # ownership_conflict on stop_is_current
blocks/stop/v3/supervisor.py          # Ownership scan; MQTT settlement capture
dashboard/server.py                   # Locked operator settlement on history sync
tests/test_inspect_spread_position.py
tests/test_expiry_settlement.py
tests/test_shared_stop_per_tranche.py # NEW
tests/test_broker_sync.py
tests/test_stop_fill_long_close.py
tests/adhoc_integration.py            # sync-broker-stop --apply
scripts/repair_orphaned_stops.py      # NEW
changes/LIVE_SESSION_2026-07-07.md
changes/SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md
```

---

## 2026-07-07 — F-9 preflight fix (Tasty qty-direction) + ms-185 close failure

**Status:** superseded by [EOD summary above](#2026-07-07-eod--jul-7-live-session-f-9-close-shared-stop-p0-eod-settlement-breach-review) — kept for grep/history

**Tests:** `uv run pytest tests/test_inspect_spread_position.py -q` → 8 passed

### What changed

- `brokers/tastytrade_broker.py` — `_signed_position_qty()` normalizes Tasty `quantity` + `quantity_direction` to signed contracts before `inspect_spread_position()` closable check
- `tests/test_inspect_spread_position.py` — ms-185 case (7425 short / 7400 long, qty 6)

### Why

ms-185 manual close @ 11:17 CT: stop cancelled, spread close blocked with `preflight_mismatch`. Tasty reports short leg as `quantity=6, quantity-direction=Short` but F-9 expected `short_qty < 0`.

### Related

- [LIVE_SESSION_2026-07-07.md](LIVE_SESSION_2026-07-07.md)

---

## 2026-07-07 (earlier) — V3 live validation: open/stop OK; manual close blocked (F-9)

**Status:** superseded by [EOD summary](#2026-07-07-eod--jul-7-live-session-f-9-close-shared-stop-p0-eod-settlement-breach-review)

### What validated (vs Jul 6 incident)

- [x] Entry → fill → stop placement (ms-184, ms-185)
- [x] Breach arm / watch without false software exit on open
- [x] ms-184 exited cleanly via **exchange stop** (did not hit manual-close preflight)
- [ ] Manual dashboard close — **failed** ms-185 @ 11:17 CT (fixed same day)

---

## 2026-07-06 (late) — Manual spread dashboard dedupe + operator cleanup

**Status:** implemented (code); operator trade files cleaned locally  
**Tests:** `uv run pytest tests/test_build_manual_trades.py -q` → pass

### What changed

| Item | Summary |
|------|---------|
| **Dashboard dedupe** | `load_dashboard_manual_trades()` keys by `lot_side`, keeps newest archive — not by filename |
| **Regression test** | `test_duplicate_history_archives_dedupe_by_lot_side` in `tests/test_build_manual_trades.py` |

### Findings / learnings

- **Why duplicates appeared:** Re-seeded `ms-99`/`ms-100` test fixtures + V3 close/finalize each wrote a **new** JSON under `trades/history/MANUAL_SPREAD/` (different timestamps in filename). Dashboard merged all “closed today” history files but deduped only by **filename** → one row per archive (9 files = 9 rows).
- **MEIC hanging:** Jul 6 `11-00` legs were `status: closed` but still in `trades/active/MEIC_IC/`; session CSV still `entered`. Grid showed them as live until active files removed.
- **Ctrl+C:** Stale `runtime/locks/` for dead PIDs (stop_monitor, streamer, market_data) — clear with `release_lock` or delete lock file if PID dead. `check_stop_monitor.py` → 0 processes.

### Operator cleanup (local, not in git)

- Removed `ms-176` active (cancelled on Tasty), `11-00` MEIC from active
- Removed duplicate `ms-99`/`ms-100` history archives from tonight’s test runs
- Session CSV: `ms-176_P` → `cancelled`; `11-00_*` → `closed` with history paths

### Files touched (code)

- `manual_spread/entry.py`
- `tests/test_build_manual_trades.py`

---

## 2026-07-06 (evening) — ChatGPT pre-live follow-up review

**Status:** implemented  
**Tests:** `uv run pytest tests/ -q` → **282 passed**

### What changed

| Item | Summary |
|------|---------|
| **Unknown preflight** | `place_spread_close_order()` blocks `unknown` position state; only `allow_unverified_emergency_close=True` bypasses (operator emergency only) |
| **AlertListener session** | `blocks/stop/run.py` reuses `get_shared_broker()` session/account — no second OAuth session |
| **Start bot lock** | `/api/start_bot` checks `runtime/locks/launcher.lock` via `active_lock_pid()` → 409 `already_running_external_lock` |
| **Phase 3 gating** | Phase 3 SPX proximity evaluated before option-leg breach arm; not blocked by `breach_watch` `no_prices` |
| **Tests** | 8 new tests across `test_v3_incident_fixes.py`, `test_broker_hardening.py` |

### Findings / learnings

- `unknown` position from failed REST preflight must **block** spread close — same hazard class as `flat` (accidental open).
- Emergency override is explicit opt-in only — not for software-breach or duplicate recovery paths.
- Phase 1 breach still requires option-leg MQTT; Phase 3 only needs SPX + time + working stop.

### Files touched

- `brokers/tastytrade_broker.py`, `brokers/base.py`
- `blocks/stop/v3/recovery.py`, `blocks/stop/v3/supervisor.py`, `blocks/stop/run.py`
- `common/process_lock.py` (`active_lock_pid`)
- `dashboard/server.py`

---

## 2026-07-06 — V3 false-breach incident repair + broker hardening

**Status:** implemented (live validation pending)  
**Tests:** `uv run pytest tests/ -q` → **274 passed**

### Issues found (live session)

| Time (CT) | Issue | Impact |
|-----------|-------|--------|
| Pre-open | Weekend test fixtures `ms-99`, `ms-100` left in `trades/active/` | Cluttered active-trade scan; removed before open |
| 09:25 | Dashboard broker spam on 2–3s poll | Fresh OAuth session every poll; partial fix same day |
| 10:59 | **V3 false breach on 11-00 IC** | Immediate close → duplicate close on flat account → accidental debit spreads at Tasty |

**Root cause (11-00 incident):** V3 treated `phase.should_activate()` (true for any open trade) as “start software breach exit,” set `close_only_mode`, recovery routed `breach_*` to `ManualKillHandler`, duplicate-close guards failed.

**Mitigation during fix window:** `STOP_MONITOR_ENGINE=v2`

### What changed — V3 incident repair

| ID | Summary | Primary files |
|----|---------|---------------|
| F-3 | `PhaseAction` enum + `evaluate()` — separate watch vs exit | `blocks/stop/phases.py` |
| F-4 | `resolve_exit_recovery_route()` — breach recovery ≠ manual kill | `blocks/stop/v3/recovery.py`, `supervisor.py` |
| F-8 | Lifecycle gates: stop → MQTT → breach armed before breach eval | `blocks/stop/v3/supervisor.py` |
| F-5 | Duplicate-close + flat-account guards in `ManualKillHandler` | `blocks/stop/v3/handlers/manual_kill.py` |
| F-6 | `finalize_v3_exit_state()` after successful close | `blocks/stop/v3/handlers/software_breach.py` |
| F-9 | Broker preflight in `place_spread_close_order` | `brokers/tastytrade_broker.py` |
| F-10 | `display_close_prices()` for dashboard PnL dedup | `blocks/stop/close_fills.py`, `dashboard/server.py` |
| R-9 | `short_closed_at` from broker `filled_at` (30s long-leg from fill time) | `blocks/stop/monitor.py` |

**Deferred:** F-7 (MQTT gate polish), F-11 (naming cleanup), R-1 (automatic orphan cleanup at EOD)

### What changed — broker / IP hardening

| Component | Location |
|-----------|----------|
| `get_shared_broker()` — one session per process | `common/broker_factory.py` |
| REST limiter (1 req/s, burst 3) | `common/rest_limiter.py` |
| Cooldown circuit breaker (skip LOW/NORMAL 5 min) | `common/broker_cooldown.py` |
| Live orders cache (TTL 2s) | `brokers/tastytrade_broker.py` |
| Dashboard fill sync gate + lock | `dashboard/broker_fill_sync.py` |
| Process locks (`runtime/locks/`) | `common/process_lock.py` — wired in `run.py`, `blocks/stop/run.py`, streamer, market_data, dashboard |
| `GET /api/broker_health` | `dashboard/server.py` |
| Start bot 409 when launcher active | `dashboard/server.py` |
| `MEIC_ALLOW_LIVE_BROKER_TESTS` pytest gate | `common/broker_factory.py` |
| `STOP_BROKER_LANE_SIZE` default **6 → 1** | `blocks/stop/v3/config.py` |
| Shared broker callers | `run.py`, `blocks/stop/run.py`, dashboard fill sync, manual spread, GEX |

**New tests:** `tests/test_v3_incident_fixes.py`, `tests/test_broker_hardening.py`

### What changed — operator tooling

- `scripts/check_stop_monitor.py` — Python version (PowerShell `.ps1` blocked by execution policy on operator machine)
- `V2_README.md` — pre-open / EOD stop-monitor check docs

### Findings / learnings (carry forward)

**V3 control flow**
- `should_activate()` ≠ breach — it means “this phase may monitor,” not “close now”
- Scan thread decides; confirmed exits on worker threads — never start exit I/O from a misclassified scan
- Lifecycle gates mandatory before breach eval (F-8)
- Recovery must use explicit route table — F-1 “resume manual kill if `close_only_mode`” is unsafe for `breach_*`
- Duplicate close on flat account can **open** positions at Tasty — F-5/F-9 are P0
- Phase 3 had same wiring risk as Phase 1 until F-3

**Broker / infrastructure**
- Multiple `get_broker()` calls = multiple OAuth sessions = rate-limit risk → use `get_shared_broker()`
- HIGH priority for orders/cancels; LOW/NORMAL skipped during cooldown
- Orphan `blocks/stop/run.py` duplicates traffic — locks + manual check script
- F-8 stop gate uses slow path (~10s) cache, not per-scan REST (R-6)
- Long-leg chase: 30s from broker fill time, not detection time (R-9)

**Operator workflow**
- Production entry: `uv run python run.py` only (Mosquitto separate)
- Rollback: `STOP_MONITOR_ENGINE=v2` in `.env` + restart

### Operator decisions

| Topic | Decision |
|-------|----------|
| V3 go-live | P0 + F-10 tonight; V3 live after morning validation |
| F-3 | Full `PhaseAction` enum |
| F-10 | Include before live |
| R-1 orphans | Defer automation; use `check_stop_monitor.py` manually |
| IP hardening | Ship alongside V3 (independent failure domains) |

### Env knobs added / changed

```env
TT_REST_MAX_PER_SEC=1
TT_REST_BURST=3
TT_BROKER_COOLDOWN_SEC=300
TT_LIVE_ORDERS_CACHE_TTL_SEC=2
STOP_BROKER_LANE_SIZE=1
STOP_MONITOR_ENGINE=v3    # after morning validation
```

### Validation checklist (next session)

```powershell
uv run python scripts/check_stop_monitor.py   # 0 before start
uv run pytest tests/ -q                       # all green
uv run python run.py                          # single launcher
# After start: 1 stop-monitor; no immediate breach on new entry
# http://localhost:5002/api/broker_health — no cooldown, launcher lock present
```

**Red flags:** immediate close after entry; `Resuming manual kill ... breach_phase1_initial_stop`; duplicate closes; 401/429 spam; >1 stop-monitor PID.

### Deep-dive docs (same incident)

| Doc | Use when |
|-----|----------|
| [LIVE_SESSION_2026-07-06.md](LIVE_SESSION_2026-07-06.md) | Raw operator log |
| [STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md](STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md) | Full RCA + patch specs |
| [STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md](STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md) | Plain-English Q&A |

### Prior context (before Jul 6 — not duplicated here)

| Doc | Relevance |
|-----|-----------|
| [LIVE_SESSION_2026-07-02.md](LIVE_SESSION_2026-07-02.md) | Broker serializes on one event loop |
| [STOP_MONITOR_V3_DESIGN.md](STOP_MONITOR_V3_DESIGN.md) | V3 architecture |
| [STOP_MONITOR_V3_REVIEW_FIXES.md](STOP_MONITOR_V3_REVIEW_FIXES.md) | Pre-incident review |
| [V2_MODULAR_REWRITE.md](V2_MODULAR_REWRITE.md) | V2 rewrite history |

---

## Open items (rolling)

| Item | Status | Since | Notes |
|------|--------|-------|-------|
| V3 live validation | **Partial pass** | 2026-07-06 | Jul 7: open/stop/close OK after F-9; MEIC shared-stop fixed evening |
| Stop monitor MQTT hardening (P0-A/B/C) | **Fixed** | 2026-07-08 | Pushed; live OK after 13:17 restart |
| Test trade isolation (`trades/test/`) | **Fixed** | 2026-07-08 | EOD commit; exclude from History sync + dashboard |
| History totals ($1085 → $530) | **Fixed** | 2026-07-08 | `effective_filled_quantity()` + stale row purge |
| ms-187 $0.05 long-close incident | **Fixed (code)** | 2026-07-08 | REST ladder + MQTT reconnect; restart required after deploy |
| Heartbeat false “missing” alert | **Fixed** | 2026-07-08 | P0-C atomic write + read retry |
| F-9 preflight qty sign (Tasty) | **Fixed** | 2026-07-07 | ms-186 validated ~13:16 |
| Shared stop per tranche (P0) | **Fixed** | 2026-07-07 | Evening deploy; fresh JSONs next session |
| EOD settlement MQTT capture | **Fixed** | 2026-07-07 | Priority + `spx_mqtt_settlement.json` |
| Dashboard SW Breach on closed rows | **Fixed** | 2026-07-07 | Persisted snapshot + not gated on live mode |
| Tranche grid entry/exit times | **Fixed** | 2026-07-07 | MEIC exit only; Manual entry + exit |
| Option quote snapshots (`options_quotes.csv`) | **Fixed** | 2026-07-07 | 3 min MQTT snapshot, no OHLC |
| Index OHLC from MQTT ticks (not 30s poll) | **Fixed** | 2026-07-08 | Tick listeners; live validate tomorrow |
| Expanded market watch (VIX/VXN/TLT/GLD, OHLCV, SPX ladder) | **Fixed** | 2026-07-09 | Sidecar ON by default; `MEIC_SIDE_OPTION_COLLECTION=0` kills ladder only |
| Quiet terminal logging | **Fixed** | 2026-07-07 | See TERMINAL_LOGGING_QUIET.md |
| Software breach threshold (2× vs 2×+$0.20) | **Deferred** | 2026-07-07 | Afternoon breach review |
| Software breach execution (spread cap) | **Deferred** | 2026-07-07 | Large slippage on Jul 7 breach exits |
| Recovery backoff on preflight_mismatch | **Deferred** | 2026-07-07 | ms-185 API storm |
| Broker hardening live smoke | **Partial** | 2026-07-06 | Scanner + place worked |
| F-7 MQTT gate polish | Deferred | 2026-07-06 | P1 |
| F-11 naming cleanup | Deferred | 2026-07-06 | P2 |
| R-1 orphan lock automation | Deferred | 2026-07-06 | Manual `check_stop_monitor.py` |
| TESTING.md breach threshold doc | Deferred | 2026-07-07 | Says `two_x_short`; code uses `two_x_net_credit` |

### Live sign-off

| Date | Operator | V3 validated | Broker smoke | Notes |
|------|----------|--------------|--------------|-------|
| 2026-07-08 | | **pass** (post-hardening) | partial | MQTT cache + long-close fix; ms-187 post-mortem |
| 2026-07-07 | | **partial → pass** (post-fix) | partial | F-9, shared-stop P0, EOD settle; breach slippage tabled |
| 2026-07-06 | | blocked | partial | False breach incident; V3 repair shipped |
