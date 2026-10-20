# Strategy Persistent Live/Paper Modes & Trade Provenance Safety

**Date:** 2026-10-20  
**Category:** Bug Fix / Architectural Safeguard  
**Affected Components:**  
- `common/strategy_modes.py` (New persistent store `trades/strategy_modes.json`)  
- `common/broker_factory.py` (`is_strategy_paper()`)  
- `common/daily_overrides.py` (`effective_config()`, `set_overrides()`, `delete_override()`)  
- `common/manual_close.py` (`close_structure()`)  
- `strategies/loader.py` (`_build_config()`)  
- `strategies/drma_hm/strategy.py` (`run()` loop, `_load_session_state()`, `_exit_trade()`)  
- `blocks/stop/broker.py` (`resolve_broker_for_trade()`)  
- `dashboard/server.py` (`_set_strategy_control()`, `api_strategy_overrides_post()`, `api_strategy_overrides_delete()`)  
- `tests/test_strategy_modes_persistence.py`  
**Status:** Complete & Verified  

---

## 1. Problem Statement & Root Cause

### Symptoms
1. **Live/Paper Modes Reverting Every Day & Restart:** An operator configured DRMA (and specific sub-strategies such as Directional Options and Non-Directional Options) to LIVE trading yesterday (`2026-10-19`) and earlier today (`2026-10-20`). However, the LIVE setting did not persist across sessions or restarts, continually reverting back to `paper: true` defaults from `strategies.yaml`.
2. **Trade Provenance Risk During Mid-Trade Mode Switches:** While an active trade was opened in paper mode (`drma_hm_2026-10-20_credit_bear_13a39207.json`), changing strategy configuration to LIVE created a dangerous risk where manual closes via the dashboard or automated exits could misroute closing orders to the real broker (`TastyTradeBroker`) instead of remaining on paper (`JournalBroker`).

### Root Cause Analysis
1. **Daily Session Overrides Expiration:** Overrides applied via the dashboard were saved exclusively in `trades/active/<strategy>/session_{YYYY-MM-DD}.json`. When the calendar date incremented (or session state was re-armed), the system opened a new session JSON without prior overrides, causing all strategies to fall back to `strategies.yaml` defaults.
2. **Missing Persistent Mode Layer:** While MEIC had `trades/meic_settings.json` and Clone Trades had `trades/clone_trades/config.json`, standalone strategies and DRMA sub-strategies lacked a dedicated persistent storage layer for execution mode toggles.
3. **Manual Close Parameter Override:** In `common/manual_close.py`, `close_structure()` accepted `paper: Optional[bool] = None`. When `dashboard/server.py` passed `paper = _strategy_paper_flag(name)`, this passed `paper=False` for any strategy currently set to LIVE, bypassing the check for `trade.get('paper')` and threatening to route a paper trade's closing legs to the live broker.

---

## 2. Technical Architecture & Design

### Dual-Layer Execution Mode Resolution Architecture

```mermaid
flowchart TD
    UI[Dashboard UI / Toggle Button] -->|POST /control or /overrides| Srv[Dashboard Server]
    Srv --> SetModes[set_strategy_paper_mode]
    
    SetModes --> PersistFile[(trades/strategy_modes.json)]
    SetModes --> ActiveSession[(session_YYYY-MM-DD.json)]
    
    subgraph ModeResolution [Mode Resolution Hierarchy]
        Q[is_strategy_paper / effective_config / _build_config]
        Q --> CheckDaily{1. Today's Daily Override?}
        CheckDaily -->|Yes| RetDaily[Return Daily Override]
        CheckDaily -->|No| CheckPersist{2. Persistent Mode in strategy_modes.json?}
        CheckPersist -->|Yes| RetPersist[Return Persistent Mode]
        CheckPersist -->|No| CheckSpec{3. Strategy-specific Config File?}
        CheckSpec -->|Yes| RetSpec[Return File Mode]
        CheckSpec -->|No| CheckYaml{4. strategies.yaml Defaults}
        CheckYaml --> RetYaml[Return YAML Default]
    end
    
    subgraph TradeProvenanceSafety [Trade Provenance Safety Guard]
        CloseReq[Close Trigger: Manual Close or Strategy Exit]
        CloseReq --> InspectTrade{Inspect trade.get('paper')}
        InspectTrade -->|True: Paper Trade| PaperBroker[Route to JournalBroker ONLY]
        InspectTrade -->|False: Live Trade| LiveBroker[Route to TastyTradeBroker]
        InspectTrade -->|Missing / None| Fallback[Fallback to Resolved Mode]
    end
```

---

## 3. Detailed Implementation

### 1. `common/strategy_modes.py` (New Persistent Store)
- Created dedicated persistence engine writing atomically to `trades/strategy_modes.json` with temporary file creation and 8-attempt Windows file replace retries.
- Key functions:
  - `strategy_modes_path(root) -> str`: Resolves path to `trades/strategy_modes.json`.
  - `get_strategy_paper_mode(strategy_name, root) -> Optional[bool]`: Resolves paper mode for master strategies and sub-strategies (`DRMA_HM_DIR`, `DRMA_HM_NONDIR`, `DRMA_HM_FUT`).
  - `get_persistent_modes_for_strategy(strategy_name, root) -> Dict[str, Any]`: Returns persistent dictionary for a strategy.
  - `set_strategy_paper_mode(strategy_name, paper, root, *, sub_key) -> Dict[str, Any]`: Saves execution mode to `trades/strategy_modes.json` and synchronizes today's `session_{YYYY-MM-DD}.json` so running strategies pick it up dynamically.
  - `delete_strategy_paper_mode(strategy_name, key, root) -> Dict[str, Any]`: Removes a persistent override to cleanly revert to YAML defaults.

### 2. `common/broker_factory.py`
- Updated `is_strategy_paper(strategy_name, default=True)`:
  - Integrated `get_strategy_paper_mode(strategy_name, root=root)` directly after daily overrides and before YAML defaults.

### 3. `strategies/loader.py`
- Updated `_build_config(entry, cfg_cls)`:
  - Queries `get_persistent_modes_for_strategy(name, root=ROOT)` and applies persistent modes onto `cfg.paper` and sub-strategy override dictionaries during initial startup.

### 4. `common/daily_overrides.py`
- Updated `effective_config(cfg, strategy_name, root)`:
  - Overlays `get_persistent_modes_for_strategy(strategy_name, root=root)` before applying today's overrides.
- Updated `set_overrides()` and `delete_override()`:
  - Automatically mirrors execution mode updates and deletions into `strategy_modes.json`.

### 5. `common/manual_close.py`
- Enforced strict trade provenance in `close_structure()`:
  ```python
  trade_paper = trade.get('paper')
  if trade_paper is not None:
      paper = bool(trade_paper)
  elif paper is None:
      from common.broker_factory import is_strategy_paper
      paper = is_strategy_paper(strategy_name, default=True)
  ```
  - Regardless of the strategy's current live/paper mode, an active trade entered on paper will NEVER send a closing order to the exchange.

### 6. `strategies/drma_hm/strategy.py`
- In `run()` loop: Dynamically queries `get_persistent_modes_for_strategy()` every polling cycle so persistent mode toggles take effect on the next entry without needing bot restarts.
- In `_exit_trade()`: Enforced trade provenance safety so `trade.get('paper')` strictly determines closing broker resolution.
- In `_load_session_state()`: Guaranteed that session dictionaries always preserve `'overrides'` key.

### 7. `blocks/stop/broker.py`
- Updated `resolve_broker_for_trade()` to honor `state.get('paper') is True/False` explicitly before checking current strategy configuration.

### 8. `dashboard/server.py`
- Synchronized `_set_strategy_control()`, `api_strategy_overrides_post()`, and `api_strategy_overrides_delete()` to update `strategy_modes.json`.

---

## 4. Automated Verification & Test Results

### 1. New Test Suite: `tests/test_strategy_modes_persistence.py`
- `test_set_and_get_strategy_paper_mode`: Validates set and get operations across toggles.
- `test_drma_sub_strategy_independent_modes`: Validates that sub-strategies can be independently live/paper.
- `test_delete_mode_reverts_to_default`: Validates override deletion and YAML reversion.
- `test_is_strategy_paper_integration`: Validates `is_strategy_paper` integration with persistent storage.
- `test_effective_config_applies_persistent_mode_on_new_day`: Validates that on a new date with zero overrides, the persistent mode is correctly applied.
- `test_build_config_loads_persisted_mode`: Validates `strategies.loader` integration.
- `test_trade_provenance_safety_manual_close`: Validates that closing an open paper trade when strategy is flipped to LIVE routes to JournalBroker and never touches TastyTradeBroker.
- `test_trade_provenance_safety_strategy_exit`: Validates that `_exit_trade` routes paper trades to JournalBroker even when strategy is set to live.

### Test Output:
```bash
$ uv run pytest tests/test_strategy_modes_persistence.py tests/test_drma_hm.py tests/test_drma_hm_dashboard.py
============================= 43 passed in 15.80s ==============================
```
