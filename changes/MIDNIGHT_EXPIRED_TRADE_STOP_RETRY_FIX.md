# Midnight Expired-Trade Stop Retry — Targeted Fix

**For:** Implementation and operator review  
**Date:** 2026-07-09  
**Incident:** July 9 ~00:00–02:14 CT — `stop_monitor` retried stop placement on July 8 `SPXW` symbols; TastyTrade rejected with `instruments_stopped_trading`.  
**Related:** `logs/launcher_2026-07-08_181744.log`, `meic0dte/logs/stop_monitor.log`, `trades/active/MEIC_IC/*_20260708*.json`

---

## Part 1 — Problem in one sentence

After midnight Central, the broker-action freeze lifted on **yesterday's** expired 0DTE trades because it only checked **time-of-day** (`>= 15:00`), not **calendar expiry**, so `stop_monitor` tried to replace cancelled day-stops on symbols that had already stopped trading.

---

## Part 2 — What happened (July 8–9 session)

| Time (CT) | What happened |
|-----------|----------------|
| **18:17** | `run.py` restarted **after** 3 PM close (`launcher_2026-07-08_181744.log`). |
| **18:17–23:59** | July 8 trades stayed `open` in `trades/active/`. Broker freeze worked (clock `>= 15:00`, expiry `== today`). |
| **00:00** | TastyTrade expired day stop orders → JSON cleared `active_stop` (`own_stop_terminal_at_broker`). |
| **00:00** | Clock rolled to `00:00` → `is_after_market_close_ct()` became **False** → freeze lifted. |
| **00:00–02:14** | `_ensure_stop_for_filled_qty()` retried `place_stop_order` every ~60s on `260708` symbols. Broker returned `instruments_stopped_trading`. |
| **02:14** | Operator stopped `run.py`. |

**Why launcher ran overnight:** `run.py` used legacy `crossed_market_close()` (time-only, no calendar rollover). Session started at 18:17 (> 15:00) so shutdown never fired; after midnight `now.time() < 15:00` kept it false on the next day too.

**Why morning cleanup did not help yet:** `session_cleanup` morning archive only runs at **08:20 CT** and only moves `expiry < today`. Trades were still visible and monitorable between midnight and 08:20.

---

## Part 3 — Root cause (two time-only bugs)

### A. `trade_past_0dte_close` — time-only gate

```python
# common/market_hours.py (current)
return trade_expiry_on_or_before_today(...) and is_after_market_close_ct(now)
```

`is_after_market_close_ct()` is `now.time() >= 15:00` with **no date component**.

| When | expiry | `expiry <= today` | `time >= 15:00` | Frozen? |
|------|--------|-------------------|-----------------|---------|
| Jul 8 18:00 | Jul 8 | ✓ | ✓ | ✓ |
| Jul 9 00:01 | Jul 8 | ✓ | ✗ | **✗ (bug)** |
| Jul 9 15:01 | Jul 8 | ✓ | ✓ | ✓ |

### B. Launcher shutdown — no strategy profile (fixed)

Legacy `crossed_market_close()` was applied as a **global** rule in `run.py`, streamer, and recorder. That is wrong for a multi-instrument platform: futures and overnight runtimes must not inherit SPX cash-close shutdown.

| Session start | Now | Legacy `crossed_market_close` | `MEIC_SPX_0DTE` profile |
|---------------|-----|-------------------------------|-------------------------|
| 08:30 same day, 15:01 | same day | ✓ | ✓ |
| 08:30, next day 02:00 | next day | **✗** | ✓ |
| 18:17 same day, any time | same or next day | **✗** | ✓ |
| 18:00 futures profile | next day 02:00 | N/A | **✗** (`allow_overnight`) |

### C. Missing settlement before broker retry

When broker cleared stops at midnight, monitor saw `status=open`, `active_stop=null`, `stop_quantity=0` and treated the trade as needing a fresh exchange stop — with no check that option expiry cutoff had passed and settlement should apply instead.

---

## Part 4 — Design goals (regression guardrails)

1. **Do not change** normal intraday stop placement for **current-day** trades before 15:00 CT.
2. **Do not break** `MANUAL_SPREAD` active directory handling (`iter_active_trade_paths()` covers both trees).
3. **Do not archive** same-day active trades before EOD review unless they are already safely settled/closed.
4. **Keep public function names** where possible; add `runtime_should_stop_for_session` for launcher policy. Legacy `crossed_market_close()` stays time-only for backward compatibility — **do not** use it for MEIC launcher shutdown.
5. **Do not apply MEIC session shutdown** to generic streamer, market data recorder, dashboard, or future futures engines — parent launcher owns their lifecycle.
6. **Prefer shared helpers** over duplicating logic in v2 `MonitorRunner` and v3 `StopSupervisor`.
7. **Broker rejections** (`instruments_stopped_trading`) are a **backstop**, not the primary fix.

---

## Part 5 — Solution overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Layer 1: market_hours.trade_past_0dte_close (date-aware)       │
│  Layer 2: runtime_session.runtime_should_stop_for_session       │
│           └─ MEIC_SPX_0DTE profile in run.py trading loop only  │
│  Layer 2b: broker_action_window.broker_actions_allowed_for_trade│
│           └─ MEIC_SPX_OPTIONS_RTH_ACTIONS in stop_monitor only  │
│  Layer 3: expiry_gate.try_settle_or_freeze_trade (shared)       │
│           ├─ MonitorRunner / StopSupervisor (before monitor)    │
│           └─ StopMonitor (before any broker call)               │
│  Layer 4: broker error classifier (stopped-trading terminal)    │
│  Layer 5: session_cleanup EOD/morning settlement pass             │
└─────────────────────────────────────────────────────────────────┘
```

**Separation of concerns:**

| Module | Question |
|--------|----------|
| `runtime_session` | Should this **process/trading loop** keep running? |
| `broker_action_window` | May this **strategy** send broker orders right now? |
| `trade_past_0dte_close` | Has this **trade's option expiry** cutoff passed? |

**Not in scope for Layer 2:** `streaming/publish_tastytrade.py`, `market_data/recorder.py`, dashboard — these exit when the parent launcher terminates them, not on MEIC cash close.

**Layer 2b:** Process may run overnight and quotes may update, but MEIC/SPX options must not place broker orders outside **08:30–15:00 CT**.

---

## Part 6 — Required changes (by file)

### 6.1 `common/market_hours.py`

**Change `trade_past_0dte_close(state, filename='', now=None)`**

Replace the `is_after_market_close_ct()`-only composition with date-aware logic:

| Condition | Result |
|-----------|--------|
| expiry missing (`trade_expiry_date` returns `None`) | `False` |
| expiry > today | `False` |
| expiry < today | `True` (any clock time) |
| expiry == today and `now.time() >= 15:00` CT | `True` |
| expiry == today and `now.time() < 15:00` CT | `False` |

**Implementation sketch:**

```python
def trade_past_0dte_close(state, filename='', *, now=None) -> bool:
    from meic0dte.app.utilities import central_now
    now = now or central_now()
    expiry = trade_expiry_date(state, filename)
    if expiry is None:
        return False
    today = now.date()
    if expiry > today:
        return False
    if expiry < today:
        return True
    return now.time() >= time(MARKET_CLOSE_HOUR_CT, MARKET_CLOSE_MINUTE_CT)
```

**Optional helper (internal, not required to be public):**

```python
def trade_expiry_cutoff_reached(state, filename='', *, now=None) -> bool:
    """Alias semantics aligned with expiry_settlement.settlement_cutoff_reached."""
    ...
```

Keep `is_after_market_close_ct()` unchanged for callers that genuinely need “clock past 3 PM today” only. Do **not** use it alone for prior-day expiry checks.

**Call sites that benefit automatically:**

- `blocks/stop/monitor.py` → `_0dte_past_market_close()` → `_broker_actions_frozen()`
- `blocks/stop/v3/supervisor.py` → `_scan_open_slot()` early return
- `blocks/entry/runner.py` (if any 0DTE entry gates use this)

---

### 6.2 `common/runtime_session.py` — strategy/runtime shutdown profiles

**Do not** extend `crossed_market_close()` as a global platform rule. Futures and overnight instruments will use their own calendars.

**New module:** `common/runtime_session.py`

```python
@dataclass(frozen=True)
class RuntimeSessionProfile:
    name: str
    allow_overnight: bool
    close_hour: int = 15
    close_minute: int = 0

MEIC_SPX_0DTE = RuntimeSessionProfile(
    name='MEIC_SPX_0DTE',
    allow_overnight=False,
)

FUTURES_OVERNIGHT = RuntimeSessionProfile(
    name='FUTURES_OVERNIGHT',
    allow_overnight=True,
)

def runtime_should_stop_for_session(
    session_start: datetime,
    now: Optional[datetime] = None,
    *,
    profile: RuntimeSessionProfile = MEIC_SPX_0DTE,
) -> bool:
    if profile.allow_overnight:
        return False
    close = time(profile.close_hour, profile.close_minute)
    if session_start.time() >= close:
        return True
    if now.date() > session_start.date():
        return True
    return now.time() >= close
```

| Profile | `allow_overnight` | Stop behavior |
|---------|-------------------|---------------|
| `MEIC_SPX_0DTE` | `False` | Post-15:00 start → stop immediately; pre-close start → stop at 15:00 or calendar rollover |
| `FUTURES_OVERNIGHT` | `True` | Never stop merely because clock is past 15:00 CT |

**`run.py` — apply only to MEIC trading loop:**

```python
from common.runtime_session import MEIC_SPX_0DTE, runtime_should_stop_for_session

# inside main() while loop:
if runtime_should_stop_for_session(session_started, now, profile=MEIC_SPX_0DTE):
    log.info('MEIC SPX 0DTE session end — shutting down trading runtime.')
    break
```

When the loop breaks, existing `finally` still terminates streamer, `stop_monitor`, and market data recorder. Infrastructure does not self-stop on cash close.

**`run.py` force/integration bypass (unchanged):**

| Mode | Bypass |
|------|--------|
| `--force` + `--tranche-now` | `main()` returns early |
| `--integration-tranche` | `no_stop_monitor`, `once`, `force` |
| `--integration-session` | separate code path |

**Remove MEIC shutdown from generic infrastructure:**

| Component | Change |
|-----------|--------|
| `streaming/publish_tastytrade.py` | Remove `crossed_market_close` self-stop; parent owns lifecycle |
| `market_data/recorder.py` | Remove `crossed_market_close` self-stop; parent owns lifecycle |
| Dashboard | No session shutdown (unchanged) |

**Legacy `crossed_market_close()` in `meic0dte/app/utilities.py`:**

Keep unchanged (time-only, same-day). Mark as legacy in docstring. Do **not** use for MEIC launcher or overnight platform components.

Future futures launcher would use:

```python
runtime_should_stop_for_session(start, now, profile=FUTURES_OVERNIGHT)  # always False from cash close
# + futures-specific session calendar elsewhere
```

---

### 6.2b `common/broker_action_window.py` — strategy broker-action window

**Separate from `runtime_session`:** the process may run overnight; MEIC/SPX options broker orders are **RTH-only** (08:30–15:00 CT).

```python
@dataclass(frozen=True)
class BrokerActionProfile:
    name: str
    allow_broker_actions_overnight: bool
    start_hour_ct: int = 8
    start_minute_ct: int = 30
    end_hour_ct: int = 15
    end_minute_ct: int = 0

MEIC_SPX_OPTIONS_RTH_ACTIONS = BrokerActionProfile(...)
FUTURES_OVERNIGHT_ACTIONS = BrokerActionProfile(allow_broker_actions_overnight=True)
```

**`broker_actions_allowed_for_trade(state, now, profile=...)` priority:**

1. `trade_past_0dte_close` → `(False, "expired_option")` — settlement/freeze path, not overnight pause
2. `profile.allow_broker_actions_overnight` → `(True, "allowed")`
3. Inside RTH window → `(True, "allowed")`
4. Outside RTH window → `(False, "outside_meic_spx_broker_action_window")`

**Outside MEIC RTH window — do not call broker; mark pause:**

```json
{
  "broker_actions_paused": true,
  "broker_actions_pause_reason": "outside_meic_spx_broker_action_window",
  "stop_rearm_pending": true
}
```

Set `stop_rearm_pending` only when protection is incomplete (`filled_quantity > stop_quantity`, missing `active_stop`, etc.).

**When window reopens (≥ 08:30 CT):** clear pause markers; if `stop_rearm_pending` or protection incomplete, call `_ensure_stop_for_filled_qty()`; clear `stop_rearm_pending` after `stop_is_current`.

**Wired in `blocks/stop/monitor.py` before:** `_on_load`, `_poll_once`, `_ensure_stop_for_filled_qty`, `_place_short_stop`, `replace_with_spread_close`, `replace_with_limit_close`, `execute_spx_proximity_close`, `_recover_closing_on_load`, `_drain_fill_queue`.

**1DTE at 02:00 CT example:** not expired, quotes may update, trade stays `open`, broker paused with `stop_rearm_pending` if unprotected. At 08:31 CT, stop may be re-armed.

**Do not apply `MEIC_SPX_OPTIONS_RTH_ACTIONS` to futures profiles** — use `FUTURES_OVERNIGHT_ACTIONS` when futures strategies are added.

---

### 6.3 Shared expiry gate — new `blocks/stop/expiry_gate.py` (recommended)

Centralize settlement/freeze so v2 runner, v3 supervisor, and `StopMonitor` share one path.

```python
"""Expiry cutoff: settle closed trades or freeze broker actions."""

from typing import Any, Dict, Literal, Optional, Tuple

Outcome = Literal['ok', 'settled', 'frozen', 'already_closed']

def try_settle_or_freeze_trade(
    state: Dict[str, Any],
    *,
    path: str = '',
    root: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Tuple[Outcome, Dict[str, Any]]:
    """
    If settlement cutoff not reached → ('ok', state) unchanged.
    If already closed → ('already_closed', state).
    If cutoff reached and SPX settlement available → mutate state to closed/expiry_settlement.
    If cutoff reached but SPX missing → mark frozen/pending, clear stops, no broker.
    """
```

**Cutoff check:** reuse `common.session_cleanup.trade_expiry_date()` + `common.expiry_settlement.settlement_cutoff_reached(expiry, now=now)`.

**Settlement path (SPX available):**

1. `spx = ensure_spx_settlement_close(expiry, root=root)` (or `get_spx_settlement_close` if already persisted)
2. `settled = compute_settled_pnl(state, spx, now=now)`
3. If `settled` is not `None`, apply fields:

| JSON field | Value |
|------------|-------|
| `status` | `"closed"` |
| `close_mechanism` | `"expiry_settlement"` |
| `settled_at_expiry` | `true` |
| `short_close_price` | from `settled` |
| `long_close_price` | from `settled` |
| `close_debit` | from `settled` |
| `pnl` | from `settled` |
| `spx_close` | from `settled` |
| `active_stop` | `None` |
| `stop_quantity` | `0` |
| `spread_close_order_id` | `None` |
| `long_close_order_id` | `None` |
| `broker_actions_frozen` | `None` or remove |
| `expiry_settlement_pending` | `None` or remove |

4. Append `stop_history` entry:

```python
state_mod.append_stop_history(
    state,
    action='settled',
    order_id=None,
    price=None,
    phase=0,
    reason='expiry_settlement',
    spx_price_at_event=spx,
)
```

**Pending/freeze path (SPX not available yet):**

| JSON field | Value |
|------------|-------|
| `broker_actions_frozen` | `true` |
| `expiry_settlement_pending` | `true` |
| `broker_actions_disabled_reason` | `"expired_option"` |
| `active_stop` | `None` |
| `stop_quantity` | `0` |
| `spread_close_order_id` | `None` |
| `long_close_order_id` | `None` |

Do **not** set `status=closed` until settlement numbers exist (keeps dashboard honest).

Return `('frozen', state)`.

---

### 6.4 `blocks/stop/monitor.py` — call gate before broker actions

Add `_maybe_settle_or_freeze_expired(self) -> bool` wrapping `try_settle_or_freeze_trade`. Return `True` when caller should **skip further broker work** this tick (`settled`, `frozen`, `already_closed`).

**Insert at top of:**

| Method | Behavior if gate returns True |
|--------|-------------------------------|
| `_on_load()` | save state, return early (before `_ensure_stop_for_filled_qty`) |
| `_poll_once()` | return immediately |
| `_place_short_stop()` | return `False` |
| `replace_with_spread_close()` | return without placing |
| `replace_with_limit_close()` | return without placing |
| `execute_spx_proximity_close()` | return without placing |
| `_recover_closing_on_load()` | skip recovery broker calls |

**Order inside `_on_load()`:**

1. `_maybe_settle_or_freeze_expired()` → return if handled
2. existing `_broker_actions_frozen()` check (still useful for same-day after 3 PM before settlement path runs on incomplete fills)
3. rest of load recovery

**Order inside `_poll_once()`:**

1. `_maybe_settle_or_freeze_expired()`
2. `_broker_actions_frozen()`
3. existing logic

This preserves intraday behavior: on expiry day before 15:00, cutoff not reached → gate returns `ok` → normal stop placement continues.

---

### 6.5 `blocks/stop/runner.py` — `MonitorRunner.add()`

Before constructing `StopMonitor`, load state and run `try_settle_or_freeze_trade`:

```python
def add(self, json_path: str) -> None:
    st = state_mod.load_state(json_path)
    outcome, st = try_settle_or_freeze_trade(st, path=json_path)
    if outcome in ('settled', 'frozen', 'already_closed'):
        state_mod.save_state(json_path, st)
    if outcome in ('settled', 'frozen', 'already_closed'):
        log.info('Skip monitor for %s — expiry %s', json_path, outcome)
        return
    # existing status / partial-fill gates...
```

---

### 6.6 `blocks/stop/v3/supervisor.py` — `_discover_slots()`

Mirror runner gate **before** `TradeSlot.from_loaded`:

```python
outcome, st = try_settle_or_freeze_trade(st, path=path)
if outcome != 'ok':
    save_state(path, st)
    continue  # do not create slot
```

Also call `_maybe_settle_or_freeze_expired` at the top of `_scan_open_slot()` via legacy monitor (belt-and-suspenders with monitor.py changes).

---

### 6.7 Broker stopped-trading classifier — new `brokers/trading_halted.py` (or `common/broker_errors.py`)

```python
_STOPPED_TRADING_MARKERS = (
    'instruments_stopped_trading',
    'stopped trading',
    'Stopped symbols',
)

def is_instruments_stopped_trading_error(message: str) -> bool:
    msg = (message or '').lower()
    return any(m.lower() in msg for m in _STOPPED_TRADING_MARKERS)
```

**Handle in `StopMonitor` when `OrderResult.success` is False:**

| Call site | On stopped-trading error |
|-----------|--------------------------|
| `_place_short_stop()` | call `_mark_expired_broker_disabled(reason='instruments_stopped_trading')`, **do not** set `_stop_place_backoff_until` for retry |
| `replace_with_spread_close()` | same terminal mark |
| `place_spread_close_order` failures in handlers | same |

`_mark_expired_broker_disabled` should set the same frozen/pending fields as §6.3 pending path and log once.

**Important:** skip the 60s backoff (`_stop_place_backoff_until`) for terminal errors — that backoff caused the overnight minute-by-minute retry storm.

---

### 6.8 `common/session_cleanup.py` — EOD/morning settlement pass

Add `settle_expired_active_trades(active_dir, root, today, logger)`:

```
for each JSON in active_dir (MEIC + MANUAL):
  if status in (open, closing) and settlement_cutoff_reached(expiry):
    try_settle_or_freeze_trade(...)
    save
```

**Invoke from `run_session_cleanup`:**

| Mode | When | Archive? | Settle? |
|------|------|----------|---------|
| `eod` | 15:30 CT | No (unchanged) | **Yes** — settle if SPX available; else mark pending |
| `morning` | 08:20 CT | Yes (`expiry < today`) | **Yes first** — settle remaining before archive |

Morning order:

1. `settle_expired_active_trades(meic_active)`
2. `settle_expired_active_trades(manual_active)`
3. existing `archive_active_trades(...)` (unchanged policy: `expiry < today`)

EOD keeps trades in `active/` for dashboard review but may set `status=closed` + `expiry_settlement` when SPX is available — same as manual dashboard settlement behavior.

Wire `ensure_spx_settlement_close(today)` before settlement loop on EOD (already called for history sync).

---

## Part 7 — Tests

### 7.1 `tests/test_market_hours.py` — extend `trade_past_0dte_close`

| Test case | expiry | now | Expected |
|-----------|--------|-----|----------|
| `test_yesterday_expiry_midnight` | 2026-07-08 symbols | 2026-07-09 00:01 CT | `True` |
| `test_today_before_close` | 2026-07-09 | 2026-07-09 14:59 CT | `False` |
| `test_today_at_close` | 2026-07-09 | 2026-07-09 15:00 CT | `True` |
| `test_future_expiry` | 2026-07-10 | 2026-07-09 16:00 CT | `False` |
| `test_missing_expiry` | no symbols | any | `False` |

Keep existing tests; update any that assumed midnight unfrozen.

### 7.2 `tests/test_runtime_session.py` — profile shutdown

| Test case | profile | session_start | now | Expected |
|-----------|---------|---------------|-----|----------|
| `test_meic_post_close_start_stops` | `MEIC_SPX_0DTE` | 18:17 | 18:20 same day | `True` |
| `test_meic_next_calendar_day_stops` | `MEIC_SPX_0DTE` | 08:30 Jul 8 | 02:00 Jul 9 | `True` |
| `test_meic_same_day_before_close_does_not_stop` | `MEIC_SPX_0DTE` | 08:30 | 14:59 same day | `False` |
| `test_meic_same_day_at_close_stops` | `MEIC_SPX_0DTE` | 08:30 | 15:00:01 same day | `True` |
| `test_futures_post_close_start_does_not_stop` | `FUTURES_OVERNIGHT` | 18:00 | 18:30 same day | `False` |
| `test_futures_next_day_early_morning_does_not_stop` | `FUTURES_OVERNIGHT` | 18:00 Jul 8 | 02:00 Jul 9 | `False` |

**Legacy `crossed_market_close` tests** remain in `tests/test_market_close.py` (time-only, unchanged behavior).

### 7.2b `tests/test_broker_action_window.py` + `tests/test_stop_monitor_broker_window.py`

| Test case | Expected |
|-----------|----------|
| 1DTE SPX at 02:00 CT | not allowed; reason `outside_meic_spx_broker_action_window` |
| 1DTE SPX at 08:29 CT | not allowed |
| 1DTE SPX at 08:31 CT | allowed |
| Expired 0DTE at 00:01 CT | `expired_option` (not window pause) |
| Futures profile at 02:00 CT | allowed |
| Intraday SPX at 10:00 CT | allowed |
| Same-day SPX after 15:00 | `expired_option` |
| 1DTE after 15:00 | `outside_meic_spx_broker_action_window` |
| Monitor overnight 1DTE | pause markers + no `place_stop_order` |
| Monitor window reopen | `_ensure_stop_for_filled_qty` when `stop_rearm_pending` |

### 7.3 `tests/test_expiry_gate.py` (new)

Use `tempfile` + `MagicMock` broker:

**`test_expired_open_trade_does_not_place_stop`**

- Trade: Jul 8 expiry, `status=open`, `active_stop=null`, `filled_quantity=1`
- Patch `central_now` → Jul 9 00:30
- Patch `get_spx_settlement_close` → 7471.32
- Run `StopMonitor._on_load()` or one `_poll_once()`
- Assert `broker.place_stop_order` **not called**

**`test_settlement_closes_trade`**

- Same trade, SPX available
- After gate: `status=='closed'`, `close_mechanism=='expiry_settlement'`, `settled_at_expiry is True`, PnL fields populated

**`test_missing_spx_freezes_without_broker`**

- Patch `get_spx_settlement_close` → `None`
- After gate: `broker_actions_frozen`, `expiry_settlement_pending`, `status` still `open`
- `place_stop_order` not called

### 7.4 `tests/test_stop_runner_expiry_gate.py` (new)

- Place expired open JSON in temp `watch_dir`
- `MonitorRunner.add(path)` → `StopMonitor` not constructed, JSON saved as settled or frozen

### 7.5 `tests/test_broker_stopped_trading.py` (new)

- Fake broker `place_stop_order` returns `OrderResult(False, None, 'rejected', message='instruments_stopped_trading: ...')`
- One `_ensure_stop_for_filled_qty()` / `_place_short_stop` call
- Assert frozen markers set, second poll does **not** call broker again

### 7.6 Regression — keep green

- `tests/test_stop_monitor_0dte_freeze.py` — same-day after 3 PM still blocked
- `tests/test_shared_stop_per_tranche.py` — intraday stop placement unchanged
- `tests/test_expiry_settlement.py` — settlement math unchanged

---

## Part 8 — Implementation order

1. **`common/market_hours.py`** + unit tests (`trade_past_0dte_close`)
2. **`common/runtime_session.py`** + `tests/test_runtime_session.py`
3. **`common/broker_action_window.py`** + `tests/test_broker_action_window.py` + `tests/test_stop_monitor_broker_window.py`
4. **`run.py`** — wire `MEIC_SPX_0DTE` profile; remove `crossed_market_close` from streamer/recorder
5. **`blocks/stop/monitor.py`** — broker-action window gate
6. **`blocks/stop/expiry_gate.py`** + `tests/test_expiry_gate.py`
4. **`blocks/stop/monitor.py`** gate hooks + stopped-trading classifier
5. **`blocks/stop/runner.py`** + v3 `supervisor.py` pre-monitor gate
6. **`common/session_cleanup.py`** settlement pass
7. Full test suite + manual smoke:

```bash
uv run python -m unittest tests.test_market_hours tests.test_runtime_session tests.test_broker_action_window tests.test_stop_monitor_broker_window tests.test_expiry_gate tests.test_stop_runner_expiry_gate tests.test_broker_stopped_trading tests.test_stop_monitor_0dte_freeze -v
```

---

## Part 9 — Verification checklist (post-implementation)

### Automated

- [ ] All new/updated unit tests pass
- [ ] No regressions in `test_shared_stop_per_tranche`, `test_expiry_settlement`

### Manual / log review

- [ ] Restart `run.py` at 18:00 CT → MEIC trading loop exits on first iteration; streamer/recorder stop via `finally`
- [ ] Futures profile (when wired) does not stop at 18:00 or 02:00 next day
- [ ] Simulate Jul 8 open trade JSON with Jul 9 clock → no `place_stop_order` in `stop_monitor.log`
- [ ] When `trades/settlement/2026-07-08.json` (or MQTT settlement) exists → trade JSON becomes `closed` / `expiry_settlement`
- [ ] When SPX missing → trade shows frozen/pending in dashboard, no broker traffic
- [ ] Intraday Jul 9 trade before 15:00 still places initial stop normally
- [ ] `MANUAL_SPREAD` active trades follow same gate via `iter_active_trade_paths()`

---

## Part 10 — Operator notes (plain English)

**What will change for you:**

- After options expire, the bot will **stop trying to place stops** on dead symbols.
- If the 3 PM SPX settlement price is already saved, trades will show as **closed at expiry** overnight instead of sitting `open`.
- If settlement price is not ready yet, trades will show **frozen / pending settlement** — still visible on the dashboard, but no broker orders.
- Restarting the bot after 3 PM will no longer leave the **MEIC trading loop** running all night.
- Streamer and recorder follow the launcher — they are not killed independently at 3 PM when a futures runtime might still need them.

**What will not change:**

- Trades opened during the day still get exchange stops until 3 PM as today.
- Same-day trades stay in the Active list for evening review until you’re done with them (unless fully settled).
- Morning archive at 8:20 still moves **prior-day** expiry files to history.

---

## Part 11 — Files touched (summary)

| File | Action |
|------|--------|
| `common/market_hours.py` | Fix `trade_past_0dte_close` |
| `common/runtime_session.py` | **New** — `RuntimeSessionProfile`, `runtime_should_stop_for_session` |
| `common/broker_action_window.py` | **New** — `BrokerActionProfile`, `broker_actions_allowed_for_trade` |
| `blocks/stop/monitor.py` | Broker-action window gate + pause/rearm markers |
| `run.py` | MEIC trading loop uses `MEIC_SPX_0DTE` profile only |
| `streaming/publish_tastytrade.py` | Remove cash-close self-stop |
| `market_data/recorder.py` | Remove cash-close self-stop |
| `meic0dte/app/utilities.py` | Docstring only — legacy `crossed_market_close` unchanged |
| `blocks/stop/expiry_gate.py` | **New** shared settle/freeze helper |
| `blocks/stop/monitor.py` | Gate + stopped-trading terminal handling |
| `blocks/stop/runner.py` | Pre-monitor expiry gate |
| `blocks/stop/v3/supervisor.py` | Pre-slot expiry gate |
| `common/session_cleanup.py` | EOD/morning settlement pass |
| `brokers/trading_halted.py` or `common/broker_errors.py` | **New** error classifier |
| `tests/test_market_hours.py` | Extend |
| `tests/test_market_close.py` | Extend |
| `tests/test_expiry_gate.py` | **New** |
| `tests/test_stop_runner_expiry_gate.py` | **New** |
| `tests/test_broker_stopped_trading.py` | **New** |

**Out of scope for this fix:** changing tranche schedules, broker API upgrades, or archiving same-day trades before operator review.
