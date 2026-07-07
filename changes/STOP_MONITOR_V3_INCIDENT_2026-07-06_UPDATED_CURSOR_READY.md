# Stop Monitor V3 — False Breach Incident & Cursor Repair Plan

**Original incident date:** 2026-07-06  
**Updated plan date:** 2026-07-07  
**Session:** Live MEIC 11-00 tranche  
**Engine involved:** `STOP_MONITOR_ENGINE=v3`  
**Recommended live engine until fixed:** `STOP_MONITOR_ENGINE=v2`  
**Status:** V3 is **not live-ready** until all P0 fixes and acceptance tests in this document pass.

**Plain-English guide (open points & tomorrow checklist):** [STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md](STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md)

---

## 1. Executive summary

At **10:59 CT on 2026-07-06**, the bot opened the 11-00 MEIC iron condor:

- Put spread: **7515/7490**
- Call spread: **7550/7575**

The entry and exchange stops were placed correctly, but V3 then closed both sides within seconds even though there was:

- no exchange stop fill,
- no confirmed spread breach,
- no killswitch,
- no operator manual close request.

After the first legitimate close flattened each side, V3 re-enqueued another close. Because the account was already flat, the second “close” orders were interpreted by Tasty as opening orders, creating accidental debit spreads.

This was not a Tasty issue and not a market-data issue. It was a V3 control-flow and recovery-routing bug.

### Root cause in one sentence

V3 treated “Phase 1 is active for an open trade” as “a software breach exit has started,” set `close_only_mode=True`, then the recovery path routed that `breach_*` state into `ManualKillHandler`, and duplicate-close guards failed to prevent a second close on a flat account.

### Live recommendation

Keep live trading on V2 until V3 passes:

1. phase-monitoring vs exit-action separation,
2. explicit recovery route table,
3. stop-first / breach-armed lifecycle,
4. duplicate-close prevention,
5. broker-flat preflight,
6. July 6 replay regression.

---

## 2. What happened

### Timeline

| Time CT | Event |
|---|---|
| 10:59:03 | Put spread opens — order `481142910` @ `$1.00` credit |
| 10:59:04 | Put exchange stop placed — `481142920` @ `$3.30` trigger |
| 10:59:04 | Breach watch reports missing MQTT leg prices |
| 10:59:04.602 | V3 creates `Exit job kind=breach_phase1_initial_stop` for put |
| 10:59:04.937 | V3 logs `Resuming manual kill ... reason=breach_phase1_initial_stop` |
| 10:59:05.087 | Put exchange stop is cancelled |
| 10:59:05 | Call spread opens — order `481142934`; stop `481142941` @ `$1.75` |
| 10:59:06.325 | Same false-breach → manual-kill chain occurs on call side |
| 10:59:07.863 | Round 1 put close placed — `481142965` @ `$1.10` debit |
| 10:59:08.006 | Put close fills; put side is flat |
| 10:59:08.824 | Round 1 call close placed — `481142977` @ `$0.90` debit |
| 10:59:08.878 | V3 recovery route logs `resume_exit_handler` |
| 10:59:08.951 | Round 2 put close enqueued — `481142982` @ `$1.05` debit |
| 10:59:10.724 | Round 2 call close enqueued — `481143011` @ `$0.95` debit |
| 10:59:10+ | Round-2 orders on flat account become Tasty BTO/STO orders, opening debit spreads |

### Broker order impact

| Order | Side | Round | Intended effect | Actual effect |
|---|---:|---:|---|---|
| `481142965` | Put | 1 | Close short put spread | Correct close |
| `481142977` | Call | 1 | Close short call spread | Correct close |
| `481142982` | Put | 2 | Duplicate close | Opened debit put spread |
| `481143011` | Call | 2 | Duplicate close | Opened debit call spread |

---

## 3. Confirmed failure chain

The failure chain is:

```text
_scan_open_slot()
  → phase.should_activate(mon)
      # Phase1InitialStop.should_activate() is true for any status == "open"
  → _enqueue_software_breach(slot, phase)
  → SoftwareBreachHandler.run()
  → mark_exit_started()
      close_only_mode = true
      exit_handler = "breach_phase1_initial_stop"
  → next supervisor cycle
  → _scan_slot() sees close_only_mode
  → F-1 recovery branch treats it as manual-kill recovery
  → ManualKillHandler
      cancel exchange stop
      place spread close order
  → first close fills
  → slot/job state still allows re-enqueue
  → second close order sent while broker account is flat
  → Tasty interprets as BTO/STO opening debit spread
```

### The important distinction

`Phase1InitialStop.should_activate()` does **not** mean a breach happened.

It only means the phase is eligible to monitor an open trade.

The real Phase 1 breach condition is inside phase execution and requires the correct gates:

```text
status == open
active stop exists
stop quantity covers filled quantity
streamer is not stale
both MQTT leg prices are available
spread mid / mark is at or above the stop threshold
```

V3 bypassed that distinction by creating an exit job too early.

---

## 4. Design rule going forward

V3 must separate these two concepts:

```text
Phase monitoring / maintenance
≠
Exit action / close pipeline
```

### Phase monitoring / maintenance examples

These should **not** automatically set `close_only_mode`:

- Phase 1 active monitoring
- Phase 2 net-credit stop upgrade
- MQTT readiness checks
- stop placement / stop repair
- breach-watch JSON snapshot updates

### Exit action examples

These may set `close_only_mode`, but only after a real close path begins:

- operator manual close
- admin killswitch
- confirmed Phase 1 software breach
- confirmed Phase 3 SPX proximity exit
- broker position reconciliation that finalizes a close

---

## 5. Updated priority matrix

The original document listed F-3 and F-4 as blockers, with F-5/F-6 as hardening. This updated plan makes F-5/F-6 live blockers too, because the second-close hazard is what opened the accidental debit spreads.

| ID | Priority | Live blocker | Summary |
|---|---:|---:|---|
| F-3 | P0 | Yes | Stop treating `should_activate()` as an exit signal |
| F-4 | P0 | Yes | Replace broad recovery logic with explicit route table |
| F-8 | P0 | Yes | Enforce stop-first and breach-armed lifecycle |
| F-5 | P0 | Yes | Add duplicate-close and broker-flat guards |
| F-6 | P0 | Yes | Clear/neutralize exit state after successful close |
| F-9 | P0 | Yes | Add broker-adapter last-line preflight before any spread close |
| F-7 | P1 | Strongly recommended | MQTT readiness gate and better breach-watch logging |
| F-10 | P1 | Recommended | Dashboard/PnL cleanup for duplicate fills and closed slots |
| F-11 | P2 | Optional | Rename logs/classes to reduce confusion between phase action and exit action |

---

# 6. Required fixes

---

## F-3 — Separate phase monitoring from exit start

**Priority:** P0  
**Live blocker:** Yes

### Problem

`_scan_open_slot()` currently treats this as an exit trigger:

```text
for phase in self.phases:
    if phase.should_activate(mon):
        self._enqueue_software_breach(slot, phase)
        save_slot(slot)
        return
```

For Phase 1, `should_activate()` is true for every open trade. That means every newly opened trade can be routed into an exit handler.

### Required change

Remove the current behavior where `_enqueue_software_breach()` is called directly from `phase.should_activate()`.

Replace it with one of these safe patterns.

### Preferred pattern: explicit phase action result

Add a phase action result object or enum.

Example:

```python
class PhaseAction:
    NONE = "none"
    MAINTENANCE = "maintenance"
    EXIT_REQUIRED = "exit_required"
```

Each phase should return one of these:

```python
result = phase.evaluate(mon)

if result == PhaseAction.NONE:
    continue

if result == PhaseAction.MAINTENANCE:
    save_slot(slot)
    continue

if result == PhaseAction.EXIT_REQUIRED:
    self._enqueue_confirmed_exit(slot, phase)
    save_slot(slot)
    return
```

Only `EXIT_REQUIRED` may call `mark_exit_started()`.

### Minimal-diff acceptable pattern

If Cursor chooses not to add a new enum immediately:

1. Run `phase.execute(mon)` or a lightweight `phase.check_exit_required(mon)` first.
2. Do **not** call `mark_exit_started()` before that.
3. Only mark exit started if the phase explicitly confirms it initiated a close, for example:
   - `mon.state["close_mechanism"]` was set,
   - `mon.state["spread_close_order_id"]` was set,
   - `mon.state["status"]` changed to `closing`,
   - a new explicit return flag says `exit_started=True`.

### Forbidden pattern

Do not keep this shape:

```python
if phase.should_activate(mon):
    mark_exit_started(...)
    phase.execute(mon)
```

That recreates the July 6 bug.

### Phase-specific behavior

| Phase | Expected V3 behavior |
|---|---|
| `Phase1InitialStop` | Monitor only until real stop/spread breach is confirmed |
| `Phase2NetCreditUpgrade` | Maintenance only; must not set `close_only_mode` |
| `Phase3SpxProximity` | May start exit only when its real proximity/time condition is confirmed |

### Acceptance tests

Required tests:

```text
test_v3_open_trade_does_not_enqueue_breach_handler
test_v3_phase1_should_activate_is_not_exit_signal
test_v3_phase2_net_credit_upgrade_does_not_set_close_only_mode
test_v3_confirmed_phase1_breach_enqueues_exit_once
test_v3_confirmed_phase3_exit_sets_close_only_mode_once
```

---

## F-4 — Replace broad recovery with explicit route table

**Priority:** P0  
**Live blocker:** Yes

### Problem

The F-1 restart-recovery path is too broad. It treats `close_only_mode` as enough reason to resume manual kill.

That is unsafe because `close_only_mode=True` can exist with non-manual handlers such as:

```text
breach_phase1_initial_stop
phase3_spx_proximity
```

### Required change

Replace logic shaped like this:

```python
if slot.close_only_mode or exit_handler in manual_handlers:
    self._enqueue_manual_kill(...)
```

with an explicit route table.

### Required route table

| Slot state | Recovery route |
|---|---|
| `status in ("closed", "cancelled")` | no-op; remove from active scan |
| `exit_handler == "manual_close"` | resume `ManualKillHandler` |
| `exit_handler == "admin_killswitch"` | resume `ManualKillHandler` |
| `exit_handler.startswith("breach_")` and close order exists | poll existing close order only |
| `exit_handler.startswith("breach_")` and no close order exists | re-check breach state or no-op; never manual kill |
| `exit_handler == "phase3_spx_proximity"` and close order exists | poll existing close order |
| `exit_handler == "phase3_spx_proximity"` and no close order exists | resume Phase 3 exit handler only if condition still holds |
| `exit_handler == "phase2_net_credit_upgrade"` | invalid exit handler; clear or log error |
| `close_only_mode=True` and empty/unknown handler | quarantine slot; do not place order automatically |

### Recommended helper

Create a single function:

```python
def resolve_exit_recovery_route(slot) -> str:
    ...
```

Possible return values:

```text
none
poll_close_order
resume_manual_kill
resume_breach_exit
resume_phase3_exit
quarantine
```

Then `_scan_slot()` should route based on that result only.

### Logging change

Replace misleading log:

```text
Resuming manual kill on restart
```

with more specific logs:

```text
Recover route manual_close → ManualKillHandler
Recover route breach_phase1_initial_stop → poll_close_order
Recover route unknown close_only state → quarantine
```

### Acceptance tests

Required tests:

```text
test_supervisor_does_not_manual_kill_on_breach_exit_handler
test_supervisor_manual_close_still_resumes_manual_kill
test_supervisor_admin_killswitch_still_resumes_manual_kill
test_recover_route_breach_with_close_order_polls_only
test_recover_route_breach_without_close_order_does_not_place_order
test_recover_route_unknown_close_only_quarantines
test_recover_route_closed_slot_none
```

---

## F-8 — Enforce four-step lifecycle in V3

**Priority:** P0  
**Live blocker:** Yes

### Problem

V3 can evaluate or enqueue breach handling before stop placement logic inside `_scan_open_slot()`. V2’s ordering is safer because it ensures the stop first, then evaluates breach logic.

### Required lifecycle

V3 must enforce this sequence:

```text
1. Place credit spread
2. Confirm fill
3. Place/verify exchange stop
4. Arm breach monitoring only after stop + MQTT are ready
```

### Required `_scan_open_slot()` order

Use this order:

```text
A. If slot is not eligible/open/fully filled → return
B. Build or refresh legacy monitor
C. Ensure stop is current
D. If stop is not current after ensure attempt → return
E. Update breach-watch snapshot
F. If MQTT prices missing or stale → return
G. Mark breach armed if not already marked
H. Evaluate phase actions
I. Enqueue exit only for confirmed exit-required phase result
```

### Required gates before Phase 1 breach evaluation

All must be true:

```text
status == "open"
not close_only_mode
filled_quantity > 0
short_leg.fill_price > 0
long_leg.fill_price > 0
active_stop.order_id exists
stop_quantity >= filled_quantity
broker stop status is working/accepted or locally trusted as active
streamer is not stale
both leg MQTT prices exist
```

### Optional but recommended JSON state

Add:

```json
"lifecycle": {
  "stop_current_at": "...",
  "breach_armed_at": "...",
  "breach_arm_status": "armed|waiting_stop|waiting_mqtt|stale|closed"
}
```

### Recommended log

```text
Breach armed 11-00 P: stop=481142920 spread_mid=... threshold=...
```

### Acceptance tests

Required tests:

```text
test_v3_no_breach_before_stop_placed
test_v3_no_breach_before_stop_quantity_covers_fill
test_v3_no_breach_when_mqtt_missing
test_v3_no_breach_when_streamer_stale
test_v3_breach_armed_after_stop_and_mqtt
test_v3_breach_armed_log_once_per_slot
test_v3_stop_ensure_runs_before_phase_evaluation
```

---

## F-5 — Duplicate-close guards in `ManualKillHandler`

**Priority:** P0  
**Live blocker:** Yes

### Problem

After the first close filled, V3 could re-enqueue another manual kill. `ManualKillHandler` had some order-id polling behavior, but it did not have enough hard stops before placing another spread close.

### Required guard order

At the top of `ManualKillHandler.run()`, before cancelling stops or resolving quotes, add:

```python
if slot.status in ("closed", "cancelled"):
    log.info("manual_kill_skip_already_terminal path=%s status=%s", ...)
    return

if slot.state.get("exit_last_step") in ("spread_close_filled", "finalized_closed"):
    log.info("manual_kill_skip_already_finalized path=%s", ...)
    return

if slot.state.get("spread_close_order_id"):
    log.info("manual_kill_poll_existing_close path=%s order_id=%s", ...)
    self._poll_spread_close(...)
    return
```

Then add a broker-position preflight:

```python
position_state = broker.inspect_spread_position(
    short_symbol=...,
    long_symbol=...,
    expected_short_qty=...,
    expected_long_qty=...,
)

if position_state in ("flat", "not_closable", "mismatch"):
    log.error("manual_kill_skip_not_closable path=%s position_state=%s", ...)
    finalize_or_quarantine_without_order(...)
    return
```

Only after these guards may it cancel exchange stops and call `place_spread_close_order()`.

### Required behavior

| Condition | Behavior |
|---|---|
| Slot already closed | Return, no broker order |
| `exit_last_step == spread_close_filled` | Return, no broker order |
| Existing `spread_close_order_id` | Poll only |
| Broker position flat | Mark/reconcile closed or quarantine; no broker order |
| Broker quantities mismatch | Quarantine; no broker order |
| Broker position still short and closable | May place close order |

### Acceptance tests

Required tests:

```text
test_manual_kill_skips_when_status_closed
test_manual_kill_skips_when_status_cancelled
test_manual_kill_skips_when_exit_last_step_spread_close_filled
test_manual_kill_polls_existing_spread_close_id_without_second_post
test_manual_kill_skips_when_broker_flat
test_manual_kill_quarantines_when_broker_position_mismatch
test_manual_kill_places_close_only_when_broker_position_is_closable
```

---

## F-6 — Clear or neutralize exit state after successful close

**Priority:** P0  
**Live blocker:** Yes

### Problem

After a successful close, leftover fields can make recovery think an exit is still in progress.

Dangerous fields include:

```text
close_only_mode
exit_handler
exit_started_at
exit_last_step
spread_close_order_id
```

Some of these fields are useful for audit, but they must not cause active recovery after the trade is closed.

### Required change

When a spread close fill is applied and the slot becomes `closed`:

1. Set `status = "closed"`.
2. Set `closed_at`.
3. Set `exit_last_step = "finalized_closed"` or move prior step into audit history.
4. Clear active recovery triggers:
   - `close_only_mode = False`
   - `exit_handler = None`
   - `exit_started_at = None`
5. Move active close order fields into audit/history, or ensure they cannot trigger new recovery.
6. Remove slot from V3 supervisor `_slots` cache immediately.
7. Ensure closed files are moved out of active trade discovery path.

### Acceptable audit structure

Instead of leaving active fields live, use:

```json
"exit_audit": {
  "handler": "breach_phase1_initial_stop",
  "started_at": "...",
  "finished_at": "...",
  "last_step": "spread_close_filled",
  "spread_close_order_id": "..."
}
```

Active fields should be safe:

```json
"status": "closed",
"close_only_mode": false,
"exit_handler": null,
"exit_started_at": null
```

### Acceptance tests

Required tests:

```text
test_finalize_close_clears_close_only_mode
test_finalize_close_clears_exit_handler
test_finalize_close_preserves_exit_audit
test_recover_route_none_after_spread_close_filled
test_closed_slot_removed_from_supervisor_cache
test_closed_slot_not_rediscovered_from_active_dir
```

---

## F-9 — Broker-adapter last-line close preflight

**Priority:** P0  
**Live blocker:** Yes

### Problem

Even if V3 has better handler guards, the broker adapter should not transmit a spread close if the account does not have a matching closable position. This is especially important with Tasty because a close instruction on a flat account may become an opening trade.

### Required change

Add a last-line preflight inside the broker adapter method that sends spread-close orders.

Before transmitting `place_spread_close_order()`:

1. Fetch current positions.
2. Confirm short leg has closable short quantity.
3. Confirm long leg has closable long quantity.
4. Confirm quantities match expected close quantity.
5. If flat or mismatched, return a safe rejected result and do not call the live order endpoint.

### Required result shape

Return something like:

```python
OrderResult(
    ok=False,
    order_id=None,
    status="rejected_preflight",
    reason="spread_not_closable_flat_or_mismatch",
    transmitted=False,
)
```

### Required log

```text
BROKER_PREFLIGHT_BLOCKED_SPREAD_CLOSE account=... short=... long=... reason=flat
```

### Acceptance tests

Required tests:

```text
test_tasty_adapter_blocks_spread_close_when_flat
test_tasty_adapter_blocks_spread_close_when_short_leg_missing
test_tasty_adapter_blocks_spread_close_when_long_leg_missing
test_tasty_adapter_blocks_spread_close_when_quantity_mismatch
test_tasty_adapter_allows_spread_close_when_position_matches
```

---

## F-7 — MQTT readiness gate and cleaner breach-watch logging

**Priority:** P1  
**Live blocker:** Strongly recommended before live

### Problem

At entry, leg symbols may not be available in MQTT immediately. This did not cause the July 6 false exit, but it makes the first seconds after entry harder to reason about and could delay real breach detection.

### Required change

If breach-watch status is:

```text
no_prices
stale
missing_short_leg
missing_long_leg
```

then V3 should:

1. not evaluate software breach,
2. not enqueue an exit,
3. not set `close_only_mode`,
4. log at DEBUG or once-per-slot INFO,
5. keep exchange stop working.

### Acceptance tests

Required tests:

```text
test_breach_watch_missing_mqtt_does_not_enqueue_exit
test_breach_watch_stale_streamer_does_not_enqueue_exit
test_breach_watch_missing_mqtt_logs_once_per_slot
test_mqtt_ready_allows_breach_evaluation_after_stop_current
```

---

## F-10 — Dashboard and PnL cleanup

**Priority:** P1  
**Live blocker:** Recommended, not required for core trading safety

### Problem

The incident corrupted dashboard interpretation because duplicate fills and close-leg prices confused the PnL view.

### Required change

Dashboard/PnL should distinguish:

```text
intended close fill
duplicate close attempt
broker-preflight blocked close
unexpected opening trade after close
manual operator cleanup
```

### Recommended fields

Add or normalize:

```json
"close_attempts": [
  {
    "order_id": "...",
    "round": 1,
    "intent": "close",
    "result": "filled",
    "position_before": "short_spread",
    "position_after": "flat"
  }
]
```

If a later order opens a debit spread accidentally, mark it as incident cleanup, not normal MEIC close PnL.

### Acceptance tests

```text
test_dashboard_uses_first_valid_close_for_meic_pnl
test_dashboard_flags_duplicate_close_attempt
test_dashboard_excludes_incident_cleanup_from_strategy_pnl
```

---

## F-11 — Naming cleanup

**Priority:** P2  
**Live blocker:** No

### Problem

The name `SoftwareBreachHandler` is confusing because it currently behaves like a generic phase runner. That made it easier to wire phase activation into an exit pipeline.

### Recommended cleanup

Rename or split:

```text
PhaseActionHandler
ConfirmedBreachExitHandler
ManualKillHandler
Phase3ExitHandler
```

Even if files are not renamed immediately, logs should clearly say:

```text
phase_monitor
phase_maintenance
confirmed_exit
manual_operator_exit
recovery_poll_only
```

---

# 7. Cursor implementation sequence

Cursor should implement in this order:

## Step 1 — Add regression tests first

Create or update tests for July 6 behavior before patching.

Minimum red tests:

```text
test_july6_open_trade_no_false_exit
test_july6_breach_handler_not_routed_to_manual_kill
test_july6_no_duplicate_close_after_first_fill
test_july6_flat_broker_preflight_blocks_second_close
```

## Step 2 — Patch F-3

Separate phase monitoring from exit start.

Expected first pass:

```text
Normal open trade
+ stop placed
+ no MQTT prices yet
= no exit job
= no close_only_mode
= exchange stop remains working
```

## Step 3 — Patch F-4

Add explicit recovery route table.

Expected first pass:

```text
exit_handler = breach_phase1_initial_stop
+ close_only_mode = true
+ status = open
= no ManualKillHandler
```

## Step 4 — Patch F-8

Move V3 `_scan_open_slot()` into stop-first, breach-armed order.

Expected first pass:

```text
no stop current → ensure stop → return
stop current + MQTT missing → wait
stop current + MQTT ready → breach armed
breach confirmed → exit handler
```

## Step 5 — Patch F-5 and F-9

Add handler-level and broker-adapter-level close preflights.

Expected first pass:

```text
account flat
+ duplicate close request
= no order transmitted
= slot marked reconciled/quarantined safely
```

## Step 6 — Patch F-6

Clear active recovery state after successful close.

Expected first pass:

```text
spread close filled
→ status closed
→ active exit flags cleared
→ audit preserved
→ slot removed from supervisor active cache
```

## Step 7 — Patch F-7/F-10 if time permits

Add MQTT readiness logging and dashboard cleanup.

---

# 8. Final acceptance criteria

V3 may be considered paper-ready only when all P0 tests pass.

V3 may be considered live-ready only after paper validation confirms the runtime behavior below.

## Unit test commands

Run:

```powershell
python -m pytest tests/test_v3_paper_scenarios.py -q
python -m pytest tests/ -q -k "v3 or manual_kill or breach or recovery or tasty"
python -m pytest tests/ -q
```

## Paper validation checklist

| Scenario | Expected result |
|---|---|
| New MEIC entry | Spread opens, exchange stop placed, no `Exit job kind=breach_*` in first 30s |
| MQTT missing after entry | No exit, no manual kill, exchange stop remains working |
| Manual close | ManualKillHandler runs once, close fills, slot closes |
| Admin killswitch | ManualKillHandler runs once, close fills, slot closes |
| Real Phase 1 breach | Confirmed breach log, one close path only |
| Real Phase 2 upgrade | Stop upgrade only, no close_only_mode |
| Real Phase 3 exit | Phase 3 close path only, no ManualKillHandler unless explicitly designed |
| Duplicate supervisor cycle | No second close |
| Broker flat | Close attempt blocked before order transmission |
| Restart mid-manual-close | Manual close resumes correctly |
| Restart after close filled | No recovery, no new order |
| Dashboard | Uses only valid close fills for MEIC PnL |

## Required log greps

During paper session, grep:

```text
Exit job kind=breach_phase1_initial_stop
Resuming manual kill
Manual kill spread close
BROKER_PREFLIGHT_BLOCKED_SPREAD_CLOSE
exit_duplicate_ignored
Breach armed
Spread closed
recover_route
```

Expected interpretation:

| Log | Acceptable? |
|---|---|
| `Exit job kind=breach_phase1_initial_stop` immediately after open | No |
| `Resuming manual kill ... reason=breach_phase1_initial_stop` | Never acceptable |
| `BROKER_PREFLIGHT_BLOCKED_SPREAD_CLOSE` | Acceptable only in tests/recovery edge cases |
| `Breach armed` after stop + MQTT | Good |
| One `Manual kill spread close` per manual/admin exit | Good |
| More than one close order per leg | Not acceptable |

---

# 9. Live return gate

V3 can return to live only when this checklist is complete:

```text
[ ] F-3 implemented
[ ] F-4 implemented
[ ] F-8 implemented
[ ] F-5 implemented
[ ] F-6 implemented
[ ] F-9 implemented
[ ] All P0 unit tests pass
[ ] July 6 replay regression passes
[ ] One full paper session has no false breach exits
[ ] One paper manual close test passes
[ ] One paper restart-after-close test passes
[ ] Broker-flat duplicate close test proves no live order is transmitted
[ ] Dashboard shows correct strategy PnL
```

Until then:

```env
STOP_MONITOR_ENGINE=v2
```

---

# 10. Cursor prompt

Use this prompt directly in Cursor:

```text
We need to patch Stop Monitor V3 after the July 6 false-breach live incident.

Read this document completely before editing.

Main bug:
V3 currently treats phase.should_activate() as an exit signal. For Phase1InitialStop, should_activate() is true for any open trade, so V3 enqueues SoftwareBreachHandler on normal open trades. SoftwareBreachHandler calls mark_exit_started() before a real breach is confirmed, which sets close_only_mode and exit_handler=breach_phase1_initial_stop. The supervisor recovery path then sees close_only_mode and routes it into ManualKillHandler. ManualKillHandler cancels exchange stops and sends spread closes. After the first close fills, V3 can re-enqueue another close, and if the broker account is flat, Tasty may interpret the second close as BTO/STO and open a debit spread.

Implement these P0 fixes:
1. Separate phase monitoring from exit action. should_activate() must never by itself enqueue an exit or call mark_exit_started().
2. Only confirmed exit conditions may set close_only_mode or exit_handler.
3. Add an explicit recovery route table. manual_close/admin_killswitch may resume ManualKillHandler. breach_* must never route to ManualKillHandler.
4. Enforce V3 lifecycle: open/fill confirmed → stop_is_current → MQTT ready → breach armed → breach evaluation → confirmed exit.
5. Add duplicate-close guards at the top of ManualKillHandler.
6. Add broker-position preflight before any spread close order. If the account is flat or quantities mismatch, do not transmit to Tasty.
7. Clear/neutralize active exit state after a successful close while preserving audit history.
8. Add July 6 regression tests and all tests listed in this document.

Do not mark V3 live-ready until all P0 tests pass and a paper session confirms no false exit, no manual-kill route from breach_*, and no duplicate close transmission when broker is flat.
```

---

# 11. Bottom line

The original four-step model is sound:

```text
place → fill → stop → breach armed
```

The July 6 incident happened because V3 skipped the meaning of “breach armed” and converted “open trade” into “exit started.”

The updated fix standard is:

```text
No real breach, no operator kill, no confirmed Phase 3 exit
=
no close_only_mode
no ManualKillHandler
no spread close order
no broker transmission
```

That rule should be treated as non-negotiable for V3 live trading.

---

# 12. Cursor review — additions to fold into plan

*Added 2026-07-07 after doc review. Merge into main sections when implementing.*

These items came from the Jul 6 log/code review and today’s orphan-process investigation. They are not yet in Sections 5–9 above.

| ID | Addition | Suggested section | Operator | Cursor note |
|---|---|---|---|---|
| R-1 | **Single stop-monitor instance** — Launcher EOD does not kill manually started or orphaned `python -m blocks.stop.run` processes. A Jul 4 orphan ran alongside today's launcher until manually killed. | §9 | Defer — treat as one-off; check later | Use `scripts/check_stop_monitor.ps1` before open (see Q6) |
| R-2 | **EOD process checklist** — Before/after session: confirm one or zero stop-monitor PIDs, `trades/heartbeat.json` `ts` frozen when idle. | §9 | Morning pre-open cleanup tomorrow (11am kill skipped normal EOD) | Run check script + `-Kill` if count > 0 before `run.py` |
| R-3 | **Phase 3 same wiring risk** — `Phase3SpxProximityClose.should_activate()` is true for any open trade after 14:51 CT. Same bug class as Phase 1 until F-3 is fixed. | §6 F-3 | Accept for now | Covered by F-3 tonight — no separate patch |
| R-4 | **Update existing F-1 test** — see elaboration below | §6 F-4, §7 Step 1 | — | **Elaboration:** Today’s test `test_supervisor_resumes_manual_kill_on_open_close_only_restart` proves F-1 works for `exit_handler='manual_close'` (operator/dashboard kill). After F-4, the same restart path must **not** fire for `exit_handler='breach_phase1_initial_stop'`. We split into two tests: (1) manual close → still resumes `ManualKillHandler`; (2) breach handler + open + `close_only_mode` → **no** spread close, route = `resume_breach_exit` or clear state. Without this, pytest would keep encoding the Jul 6 bug as “correct.” |
| R-5 | **Confirmed breach stays async** — see elaboration below | §6 F-3, F-8 | — | **Elaboration:** V3 scans every ~250ms on the main supervisor thread. Placing/cancelling orders there blocks all trades. Today `_enqueue_software_breach` already uses `exit_pool.submit()` (background thread) — keep that for **confirmed** exits only. Wrong path (Jul 6): enqueue breach when `should_activate()` is true → `mark_exit_started()` → F-1 resumes kill on scan thread. Right path: scan thread only **decides** exit required; worker thread runs `replace_with_limit_close()` and broker I/O. |
| R-6 | **F-8 broker stop gate** — see elaboration below | §6 F-8 | — | **Elaboration:** F-8 requires “broker stop status is working” before arming breach. That may need a REST call to Tasty per trade. Doing that every 250ms × N open slots → IP throttle (Jul 6 context). Use existing **slow path** (~10s): refresh stop status there, cache on slot state (`lifecycle.breach_arm_status`). Fast scan reads cache only; if stale/missing, stay `waiting_stop` — no breach eval, no close. |
| R-7 | **F-4 breach crash recovery** — If crash after stop cancel but before spread close, route = `resume_breach_exit`. | §6 F-4 | Agreed | Implement in F-4 `resolve_exit_recovery_route()` |
| R-8 | **Code status** — P0 fixes not in repo yet. | §9 | **Fix V3 tonight** | Scope: P0 + F-10 before live tomorrow |
| R-9 | **30s long-leg delay from broker fill time** — On slow-path stop fill detect, set `short_closed_at` from Tasty fill timestamp; long chase fires at fill_time + 30s (not detection_time + 30s). | §6 F-8, long chase | **Operator rule** | Example: fill at T+0, detect at T+9 → wait 21s more |

### Proposed §9 additions (copy when ready)

```text
[ ] scripts/check_stop_monitor.ps1 — 0 or 1 process before open; 0 after EOD
[ ] EOD: heartbeat ts frozen after shutdown (wait 5s, re-read heartbeat.json)
[ ] STOP_MONITOR_ENGINE=v3 only after Section 9 + operator sign-off below
```

---

# 13. Operator decisions — questions and comments

*Fill in **Your decision / comments** for each item before implementation or live return. Leave blank if deferring.*

---

## Q1 — Tomorrow’s engine and validation plan

**Context:** Section 9 requires P0 implementation + paper validation before live. As of 2026-07-07, fixes are spec-only (not yet in code).

**Options:**

- **A.** Live MEIC on **V2** tomorrow; implement V3 fixes during/after session  
- **B.** **Paper session first** (30–60 min), then V3 live only if Section 9 checklist passes  
- **C.** V3 live tomorrow without waiting for P0 fixes *(not recommended — repeats Jul 6 risk)*

**Your decision / comments:**

```
Fix V3 tonight (P0 + F-10). V3 live tomorrow after morning check script + quick validation.
```

**Cursor read:** Not option A/B/C as written — operator chose implement tonight, go live on V3 tomorrow.

---

## Q2 — F-3 implementation style (first patch)

**Context:** Section 6 F-3 offers two patterns. Pick one for Cursor to avoid scope creep.

**Options:**

- **A.** **PhaseAction enum** (`NONE` / `MAINTENANCE` / `EXIT_REQUIRED`) — cleaner long-term, slightly larger diff  
- **B.** **Minimal-diff** — run `phase.execute()` or `check_exit_required()` first; call `mark_exit_started()` only when close is confirmed  

**Elaboration:**

| | **A — PhaseAction enum** | **B — Minimal-diff** |
|---|---|---|
| What changes | Each phase gets `evaluate(mon) → NONE \| MAINTENANCE \| EXIT_REQUIRED`. Supervisor has one clear branch. | Keep `should_activate()` / `execute()`; add guards so `mark_exit_started()` only after spread close order or `status=closing`. |
| Pros | Hard to reintroduce Jul 6 bug; tests read clearly; Phase 3 gets same pattern. | Smaller first diff; faster to type tonight. |
| Cons | More files touched (`phases.py`, supervisor, tests). | Easy to miss a code path; Phase 3 still needs same discipline. |
| Recommendation | **Use A** — you are doing full P0 + F-10 tonight; enum pays off in F-4 tests (R-4). | OK only if time-constrained. |

**Your decision / comments:**

```
(use A — PhaseAction enum; full fix tonight)
```

---

## Q3 — F-10 dashboard / PnL scope before live

**Context:** F-10 is P1 in the doc. Jul 6 dashboard showed wrong PnL from duplicate fills.

**Your decision / comments:**

```
All fixes (P0 + F-10) before V3 live tomorrow.
```

---

## Q4 — Implementation timing

**Your decision / comments:**

```
All tonight.
```

---

## Q5 — Explicit override (only if going live before checklist complete)

**Your decision / comments:**

```
N/A — fixing everything tonight per Q3/Q4.
```

---

## Q6 — Orphan / dual stop-monitor policy

**Your decision / comments:**

```
Add check/kill script — done: scripts/check_stop_monitor.ps1 + V2_README section.
Run before launcher start tomorrow morning.
```

**Commands:**

```powershell
cd MEIC-with-Dash-main-V2
.\scripts\check_stop_monitor.ps1          # list + heartbeat
.\scripts\check_stop_monitor.ps1 -Kill    # kill orphans (prompts)
```

---

## Q7 — `.env` and rollback

**Your decision / comments:**

```
Fix all tonight; STOP_MONITOR_ENGINE=v3 for tomorrow after Section 9 checklist passes.
Rollback: set v2 in .env → restart run.py
```

---

## Q8 — Anything missing from this doc?

**Your notes / corrections / priorities:**

```
(operator — add if needed)
```

---

## Sign-off (optional)

| Role | Name | Date | V3 live approved? |
|---|---|---|---|
| Operator | | 2026-07-07 | Pending — after tonight implementation + morning script check |
| Notes | | | P0 + F-10 tonight; `check_stop_monitor.ps1` pre-open |

