# Stop Monitor V3 — False Breach Incident (11:00 IC)

**Date:** 2026-07-06  
**Session:** Live MEIC 11-00 tranche  
**Engine:** `STOP_MONITOR_ENGINE=v3`  
**Operator:** Tasty positions cleaned up manually post-incident  
**Related:** [STOP_MONITOR_V3_REVIEW_FIXES.md](STOP_MONITOR_V3_REVIEW_FIXES.md) (F-1/F-2), [LIVE_SESSION_2026-07-06.md](LIVE_SESSION_2026-07-06.md)

---

## Executive summary

At **10:59 CT** on Jul 6, the bot opened the 11-00 IC (put 7515/7490, call 7550/7575), placed exchange stops correctly, then **closed both legs within ~5 seconds** with no stop fill, no spread breach, and no killswitch.

Root cause is a **V3 wiring bug**, not market data or Tasty:

1. V3 enqueues `SoftwareBreachHandler` whenever `Phase1InitialStop.should_activate()` is true — which is simply `status == 'open'` for every new trade.
2. That handler calls `mark_exit_started()`, setting `close_only_mode = true` and `exit_handler = breach_phase1_initial_stop`.
3. F-1 restart-recovery logic (shipped Jul 5) then treats `close_only_mode` as “resume manual kill” and cancels stops + sends spread closes.
4. After the first close fills, V3 **re-enqueued** manual kill (no duplicate-close guard; exit job slot freed every ~250ms). Second closes on a **flat account** were converted by Tasty to **BTO/STO**, opening accidental debit spreads.

**Immediate mitigation:** Set `STOP_MONITOR_ENGINE=v2` in `.env` and restart launcher until fixes below are implemented and tested.

---

## Incident timeline (10:59 CT)

| Time | Event |
|------|-------|
| 10:59:03 | Put spread opens — order **481142910** @ $1.00 cr |
| 10:59:04 | Exchange stop placed — **481142920** @ $3.30 trigger |
| 10:59:04 | `Breach watch 11-00 P: missing MQTT` (leg symbols not in streamer yet) |
| 10:59:04.602 | `Exit job kind=breach_phase1_initial_stop` (put) |
| 10:59:04.937 | `Resuming manual kill … reason=breach_phase1_initial_stop` (put) |
| 10:59:05.087 | Stop **481142920** cancelled |
| 10:59:05 | Call spread opens — **481142934**; stop **481142941** @ $1.75 |
| 10:59:06.325 | Same false-breach → manual-kill chain on call leg |
| 10:59:07.863 | **Round 1** put close — **481142965** @ $1.10 db (correct BTC/STC) |
| 10:59:08.006 | Put close filled; positions flat |
| 10:59:08.824 | **Round 1** call close — **481142977** @ $0.90 db |
| 10:59:08.878 | `V3 startup recovery … route=resume_exit_handler` (put) |
| 10:59:08.951 | **Round 2** put close enqueued — **481142982** @ $1.05 db |
| 10:59:10.724 | **Round 2** call close enqueued — **481143011** @ $0.95 db |
| 10:59:10+ | Round-2 orders on flat account → Tasty **BTO/STO** → debit IC opened |

### Broker order summary

| Order | Leg | Round | Effect on account |
|-------|-----|-------|-------------------|
| 481142965 | Put | 1 | Correct close (BTC/STC) |
| 481142977 | Call | 1 | Correct close (BTC/STC) |
| 481142982 | Put | 2 | Opened debit spread (BTO/STO) — flat account |
| 481143011 | Call | 2 | Opened debit spread (BTO/STO) — flat account |

Trade JSON recorded `close_mechanism: breach_phase1_initial_stop` on both legs. Dashboard PnL was wrong (garbled close-leg prices from duplicate fills).

**Log source:** `meic0dte/logs/stop_monitor.log` (launcher `logs/launcher_2026-07-06_084718.log` for entry context).

---

## Investigation — Q1: Why immediate close with no exit conditions?

**Answer: No exit condition fired.** V3 misrouted normal phase-1 monitoring into the exit pipeline.

### Failure chain

```
_scan_open_slot()
  → phase.should_activate(mon)     # true for ANY status=='open' trade
  → _enqueue_software_breach()
      → mark_exit_started()        # close_only_mode=true, exit_handler=breach_phase1_initial_stop
  → next supervisor cycle (~250ms)
  → _scan_slot() sees close_only_mode
  → "Resuming manual kill on restart"   # F-1 path — fires live, not only on restart
  → ManualKillHandler
      → cancel exchange stop
      → place_spread_close_order() (BTC/STC)
```

### Code references

**V3 enqueues breach handler on `should_activate` only:**

```389:393:blocks/stop/v3/supervisor.py
        for phase in self.phases:
            if phase.should_activate(mon):
                self._enqueue_software_breach(slot, phase)
                save_slot(slot)
                return
```

**Phase 1 `should_activate` is not a breach check:**

```37:38:blocks/stop/phases.py
    def should_activate(self, monitor: 'StopMonitor') -> bool:
        return monitor.state.get('status') == 'open'
```

Real breach detection lives inside `Phase1InitialStop.execute()` (`spread_breach_triggered`, etc.) — the same method V2 runs after `should_activate`, but V2 does **not** call `mark_exit_started()` first.

**SoftwareBreachHandler sets exit mode before knowing if a breach occurred:**

```44:58:blocks/stop/v3/handlers/software_breach.py
                if not self.slot.state.get('exit_started_at'):
                    mark_exit_started(
                        self.slot.state,
                        step=f'breach_{self.phase.name}',
                        mechanism=mechanism,
                    )
                ...
                self.phase.execute(mon)
```

**F-1 resume path treats any `close_only_mode` as manual kill:**

```444:461:blocks/stop/v3/supervisor.py
        manual_handlers = ('manual_close', 'admin_killswitch')
        exit_handler = str(slot.state.get('exit_handler') or '')
        if slot.close_only_mode or exit_handler in manual_handlers:
            ...
            if slot.status == 'open' and not self.exit_pool.has_job(slot.path):
                log.info('Resuming manual kill on restart path=%s reason=%s', ...)
                self._enqueue_manual_kill(slot, reason=str(reason))
```

### V2 contrast

In `monitor.py`, V2 uses `should_activate` to enter phase monitoring, runs `phase.execute()` in a background thread, and only acts when `execute()` detects a breach (`replace_with_limit_close`). It never sets `close_only_mode` merely because phase 1 is active.

### Contributing factor (not root cause)

`Breach watch: missing MQTT` at 10:59:04 — leg symbols not yet registered in the streamer cache right after entry. SPX index prices were live; this did **not** trigger the kill. The false breach enqueue happened regardless.

---

## Investigation — Q2: Why a second close after the first?

**Answer:** V3 re-enqueued `ManualKillHandler` after the first job finished while `close_only_mode` remained set and no guard prevented a duplicate close on a flat position.

### Evidence

Four `Resuming manual kill` log lines — two per leg:

| Time | Leg |
|------|-----|
| 10:59:04.937 | Put (round 1) |
| 10:59:06.325 | Call (round 1) |
| 10:59:08.951 | Put (round 2 — **after** round-1 fill at 08.006) |
| 10:59:10.724 | Call (round 2) |

Between put round-1 fill and round-2 enqueue:

```
10:59:08.096  Spread closed (put)
10:59:08.878  V3 startup recovery … route=resume_exit_handler
10:59:08.951  Resuming manual kill (put) → order 481142982
```

### Mechanisms

| Gap | Detail |
|-----|--------|
| **Exit pool dedup window** | `has_job()` is true only while the worker thread runs. When the first `manual_kill` job completes (~seconds), the path is removed from `_active` and the supervisor may enqueue again on the next 0.25s cycle. |
| **Resume branch** | Fires when `close_only_mode` + `status == 'open'`. Does not exclude `breach_*` exit handlers. Does not check broker position or prior successful close. |
| **ManualKillHandler** | Checks `spread_close_order_id` (poll existing order) but not `status == 'closed'`, not “position flat at broker”, not “close already finalized”. |
| **Slot rediscovery** | When a slot drops from `_slots` cache and reloads with `exit_handler` still set, `recover_route()` returns `resume_exit_handler` even mid-session. |
| **Flat-account hazard** | `place_spread_close_order()` sends BTC/STC. With no position, Tasty converts to BTO/STO → accidental debit spread. |

---

## Proposed fixes

Fixes are ordered by priority. **F-3 and F-4 are blockers** for returning to V3 live. F-5/F-6 harden against duplicate closes. F-7 is optional polish.

| ID | Priority | Summary |
|----|----------|---------|
| **F-3** | P0 | Stop enqueueing breach exit on `should_activate`; only exit on real breach |
| **F-4** | P0 | Narrow F-1 resume path — do not route `breach_*` through manual kill |
| **F-5** | P1 | Duplicate-close guards in `ManualKillHandler` |
| **F-6** | P1 | Clear exit state / block re-enqueue after successful close |
| **F-7** | P2 | Defer phase-1 breach scan until MQTT symbols registered |
| **F-8** | P1 | Enforce explicit 4-step lifecycle: fill → stop → breach armed (V3 ordering) |

---

### F-3 — Only enqueue breach handler when a breach actually occurs (P0)

**Problem:** `_scan_open_slot` treats `should_activate` (any open trade) as “start breach exit.”

**Proposed change:**

1. **Remove** the `_enqueue_software_breach` call from the `should_activate` loop in `_scan_open_slot`.
2. **Run phase monitoring inline** on the supervisor scan thread (mirror V2), OR call a new lightweight `phase.check_breach(mon) -> bool` that performs only MQTT-based breach detection without broker I/O.
3. **Enqueue `SoftwareBreachHandler` only when** breach is confirmed (same conditions as `Phase1InitialStop.execute()` lines 61–74: spread price ≥ stop threshold, or killswitch).
4. **Move `mark_exit_started()`** inside the breach response path — either at the start of `SoftwareBreachHandler` only after `execute()` calls `replace_with_limit_close`, or inside `replace_with_limit_close` / `phase.execute` when breach is confirmed.

**Alternative (minimal diff):** Keep `_enqueue_software_breach` but change `SoftwareBreachHandler` to:

- Call `phase.execute(mon)` **first** without `mark_exit_started`.
- Call `mark_exit_started` only if `execute()` set `close_mechanism` or initiated `replace_with_limit_close` (e.g. check `mon.state.get('close_mechanism')` or a return flag from `execute`).

**Risk if skipped:** Every new trade will false-exit under V3.

**Test:**

- `test_v3_open_trade_does_not_enqueue_breach_handler` — new open trade with stop placed → no `Exit job kind=breach_*` within N supervisor cycles.
- `test_v3_breach_enqueued_on_spread_threshold` — paper/mock prices above stop → breach handler enqueued once.
- Regression: existing `TestRestartMidClose` still passes for real manual kill.

---

### F-4 — Do not route `breach_*` exit handlers through manual-kill resume (P0)

**Problem:** F-1 fixed stranded manual kills but broadened the condition to **any** `close_only_mode`, including false `breach_phase1_initial_stop`. That path cancels stops and sends spread closes — correct for `manual_close` / `admin_killswitch`, wrong for software breach.

**Proposed change** in `_scan_slot`:

```python
manual_handlers = ('manual_close', 'admin_killswitch')
exit_handler = str(slot.state.get('exit_handler') or '')

# Resume manual kill only for operator-initiated exits
if exit_handler in manual_handlers or (
    slot.close_only_mode and exit_handler in manual_handlers
):
    ...

# Software breach in progress: poll closing or let breach worker run
if exit_handler.startswith('breach_'):
    if slot.status == 'closing':
        self._poll_closing(slot)
        return
    if self.exit_pool.has_job(slot.path):
        slot.exit_job_id = 'active'
        return
    # Optional: re-enqueue SoftwareBreachHandler for crash recovery only
    # (see F-6), NOT ManualKillHandler
    return
```

**Also:** Rename log line from `Resuming manual kill on restart` to `Resuming operator exit` so live vs restart is obvious in logs.

**Risk if skipped:** Even with F-3, any future bug that sets `close_only_mode` with a breach handler could still cancel stops and spread-close.

**Test:**

- `test_supervisor_does_not_manual_kill_on_breach_exit_handler` — slot with `exit_handler=breach_phase1_initial_stop`, `close_only_mode=true`, `status=open` → no `ManualKillHandler` enqueued.
- F-1 regression: `open + close_only_mode + exit_handler=manual_close` still resumes `ManualKillHandler`.

---

### F-5 — Duplicate-close guards in `ManualKillHandler` (P1)

**Problem:** Second manual kill placed spread closes after round-1 fill while account was flat.

**Proposed guards** (early return in `run()`, before cancel/place):

| Guard | Action |
|-------|--------|
| `slot.status in ('closed', 'cancelled')` | Log `manual_kill_skip_already_closed`; return |
| `spread_close_order_id` set | Poll only (`_poll_spread_close`); do not place new order (partially exists — ensure no fall-through) |
| Broker position flat for short+long symbols | Log `manual_kill_skip_flat_position`; call `_finalize_close` or mark closed without new order |
| `exit_last_step == 'spread_close_filled'` | Skip re-place |

**Broker flat check:** Compare `broker.get_positions()` (or existing position helper) for short/long leg quantities vs `stop_qty_for_state`. If both legs show zero when close is expected, do not send `place_spread_close_order`.

**Risk if skipped:** Duplicate closes on any retry/resume race; flat-account BTO/STO hazard remains.

**Test:**

- `test_manual_kill_skips_when_status_closed`
- `test_manual_kill_skips_when_broker_flat`
- `test_manual_kill_polls_existing_spread_close_id` (no second POST)

---

### F-6 — Clear exit state after successful close; tighten recovery routes (P1)

**Problem:** `close_only_mode` and `exit_handler` persist after close; slot rediscovery triggers `resume_exit_handler`; supervisor re-enqueues kills.

**Proposed changes:**

1. **`_finalize_close` / `_apply_spread_close_fill`:** Clear `close_only_mode`, `exit_handler`, `exit_started_at`, `exit_last_step` when setting `status=closed` (or document that `move_to_closed` strips them in saved JSON).
2. **`recover_route()`:** Return `None` if `status == 'closed'` (already does) **and** if `exit_last_step in ('spread_close_filled', 'phase_done:*')` with no working orders.
3. **`_discover_slots`:** Do not log/recover slots that have `status=closed` on disk (already ineligible — verify no stale active file after `move_to_closed`).
4. **Supervisor:** After `_poll_closing` applies fill → `closed`, remove slot from `_slots` immediately; do not scan again until file reappears in active (should not).

**Risk if skipped:** Intermittent duplicate closes on fast fills + 250ms scan cadence.

**Test:**

- `test_finalize_close_clears_close_only_mode`
- `test_recover_route_none_after_spread_close_filled`

---

### F-7 — MQTT symbol readiness gate (P2, optional)

**Problem:** At entry, `Breach watch: missing MQTT` logged for new leg symbols. Not the root cause of this incident, but reduces observability and could affect real breach detection timing.

**Proposed change:**

- In `_scan_open_slot`, if breach watch reports `no_prices` / missing symbols for this trade’s legs, skip breach enqueue (F-3) and log at DEBUG once per trade until symbols appear.
- Ensure `register_spread_symbols()` completes before first breach check (already called on slot discovery — verify streamer subscription latency).

**Risk if skipped:** Low for this incident; real breaches might be delayed by a few seconds after entry until MQTT catches up.

---

### F-8 — Enforce 4-step lifecycle in V3 (P1)

**Problem:** V3 `_scan_open_slot` can enqueue breach handling before stop placement in code order, and has no explicit “breach armed” gate separate from `status == open`.

**Proposed change:** See [Four-step lifecycle](#four-step-lifecycle--is-step-by-step-possible) section — reorder scan loop (stop before breach), require `stop_is_current()` + MQTT before breach logic, optional `breach_armed_at` field.

**Test:**

- `test_v3_no_breach_before_stop_placed` — open trade, no `active_stop` → no breach handler, stop placed first.
- `test_v3_breach_armed_after_mqtt_and_stop` — mock prices + stop → breach watch `ok`, then breach check runs.

---

## Operator mitigation (until fixes ship)

| Step | Action |
|------|--------|
| 1 | Set `STOP_MONITOR_ENGINE=v2` in `.env` |
| 2 | Restart launcher (stop monitor + dependent processes) |
| 3 | Confirm `trades/heartbeat.json` shows `"engine": "v2"` |
| 4 | Do **not** use V3 for live MEIC until F-3 + F-4 pass tests |
| 5 | If V3 must run in paper: watch for `breach_phase1` + `Resuming manual kill` within seconds of entry |

---

## Verification plan (post-fix)

### Unit / paper tests

```text
python -m pytest tests/test_v3_paper_scenarios.py -q
python -m pytest tests/ -q -k "v3 or manual_kill or breach"
```

New tests required: F-3, F-4, F-5, F-6 cases listed above.

### Paper / live observation checklist

| Check | Pass criteria |
|-------|----------------|
| New entry | Stop placed; **no** `Exit job kind=breach_*` in first 30s |
| Manual kill | F-1 still works: kill → spread close → closed |
| Real breach (when occurs) | `Software breach` log with spread ≥ threshold; single close path |
| No duplicate closes | Grep log: one `Manual kill spread close` per leg per exit |
| Heartbeat | `loop_count` advances; no freeze |

### Log grep (during test session)

```text
breach_phase1_initial_stop
Resuming manual kill
Manual kill spread close
exit_duplicate_ignored
Spread closed
```

---

## Four-step lifecycle — is step-by-step possible?

**Yes.** The system is already designed as a four-step pipeline. Each step has explicit gates in code. Under normal operation (especially V2), all four steps complete within **1–3 seconds** of order placement — broker latency dominates, not intentional delays.

The Jul 6 incident did **not** violate step ordering because steps ran too slowly; it violated step 4 by activating a false exit before step 3’s protections were meaningful (V3 bug).

### Target sequence

```
1. Place credit spread     →  status: pending_fill
2. Confirm fill            →  status: open, leg fill prices set
3. Place stop on short     →  active_stop.order_id, stop_quantity == filled_quantity
4. Activate breach watch   →  MQTT prices on both legs, spread vs threshold
```

### How each step works today

#### Step 1 — Place credit spread

| | |
|--|--|
| **Who** | Entry worker (`blocks/entry/meic_worker.py`) or legacy `vertical_thin.py` |
| **Action** | `broker.place_spread_order()` (STO short / BTO long, NET_CREDIT) |
| **Persistence** | `write_pending_trade_state()` → JSON with `status: pending_fill`, `open_order_id` |
| **Also** | `register_spread_symbols()` — adds legs to streamer subscription immediately after place |

Entry does **not** place a stop. Handshake comment: *"stop_monitor syncs fills; no stop on place"*.

#### Step 2 — Confirm fill

| | |
|--|--|
| **Primary path (MEIC)** | Entry worker `_poll_until_done()` polls broker every `FILL_WAIT` (5s config, but returns as soon as fill arrives) |
| **Backup path** | `sync_pending_fills()` in stop monitor — promotes `pending_fill → open` if entry handoff raced ahead |
| **Gate** | `filled_quantity > 0`, both `short_leg.fill_price` and `long_leg.fill_price` > 0 → `status: open` |
| **Stop monitor gate** | `MonitorRunner.add()` / V3 `_slot_eligible()` **skip** trades until `status == open` and `filled >= quantity` |

On full fill, entry calls `apply_stop_snapshot()` (sets 2× thresholds, marks `fully_filled`) and returns — **handoff to stop monitor**.

**Typical delay:** Sub-second for marketable limits (Jul 6 put: placed ~03.3, fill synced ~03.9 = **~600ms**). Worst case bounded by `FILL_WAIT_MAX` (5s default) in entry worker.

#### Step 3 — Place stop on short side

| | |
|--|--|
| **Who** | `StopMonitor._ensure_stop_for_filled_qty()` |
| **When called** | V2: on monitor `_on_load()` and every `_poll_once()` while `open` and `not stop_is_current()` |
| **Preconditions** | `status == open`, both leg fills > 0, `filled_quantity > 0`, no working stop already covering qty |
| **Action** | Exchange `STOP_LIMIT` on short leg at 2× short fill (phase 1) |
| **Gate helper** | `stop_is_current()` — true when `active_stop.order_id` exists and `stop_quantity >= filled_quantity` |

**V2 ordering (correct):** `_poll_once` calls `_ensure_stop_for_filled_qty()` **before** phase breach checks.

**V3 ordering (wrong):** `_scan_open_slot` enqueues breach handler **before** `_ensure_stop_for_filled_qty()` at the bottom of the function. Jul 6 stop still landed at 10:59:04.186 because `_legacy_monitor()` → `_on_load()` placed the stop on first slot attach (~270ms before the false breach at 04.602).

**Typical delay:** One broker POST after open promotion (Jul 6: **~270ms** after `pending_fill→open`).

#### Step 4 — Activate breach monitoring

| | |
|--|--|
| **Who** | `Phase1InitialStop.execute()` (V2 inline / background thread) |
| **Real breach gate** | Requires **all** of: `active_stop.order_id` present, stop type `STOP_LIMIT`, **both** MQTT leg prices available, `spread_breach_triggered(spread_mid, stop_price)` |
| **Observability** | `breach_watch` snapshot on JSON (`status`: `ok` / `near` / `no_prices` / `stale` / `breached`) |
| **If MQTT missing** | V2: `execute()` skips breach block (lines 61–75 need prices + stop); may still call `_ensure_stop_for_filled_qty` if stop missing |
| **If streamer stale** | V2/V3: breach checks frozen (`_streamer_prices_stale`) |

**Intended behavior:** Breach is **not armed** until step 3 is done and MQTT has both legs. `Phase1InitialStop.execute()` enforces this at lines 61–78 in `phases.py`.

**V3 bug:** Bypasses `execute()` breach gates entirely — enqueues exit on `should_activate` (= any open trade) and routes to manual kill.

### Jul 6 observed timings (put leg)

| Step | Time (CT) | Δ from place |
|------|-----------|--------------|
| 1. Place spread | 10:59:03.3 | — |
| 2. Fill confirmed (`pending_fill→open`) | 10:59:03.9 | ~0.6s |
| 3. Stop placed (481142920) | 10:59:04.2 | ~0.9s |
| 4. False “breach” (should not have fired) | 10:59:04.6 | ~1.3s |

Steps 1–3 ran at acceptable speed. Step 4 fired incorrectly ~400ms after the stop — not because breach was armed, but because V3 treated “open trade” as “start exit.”

At 10:59:04.190, `breach_watch` correctly reported `no_prices` (MQTT legs MISSING). A correctly gated step 4 would have **waited**; V3 did not.

### Gaps vs ideal step-by-step model

| Gap | Impact | Fix |
|-----|--------|-----|
| V3 breach enqueue on `should_activate` | Step 4 runs before step 3 logic; false exit | **F-3**, **F-4** |
| V3 `_scan_open_slot` order: breach before `_ensure_stop` | Race if `_on_load` did not run first | **F-8** — match V2 order |
| No explicit `breach_armed` state | Hard to audit lifecycle in JSON/logs | **F-8** — optional field |
| MQTT registration lag (~0.5–1s) | `breach_watch: no_prices` briefly after entry | **F-7**; entry already calls `register_spread_symbols` at place |
| V3 `sync_pending_fills` throttled to 10s | Backup fill sync slow if entry worker crashes mid-fill | Entry worker is primary; consider faster backup sync on `pending_fill` only |
| V2 runner scan interval 3s | New trade may wait up to 3s for monitor start if entry did not promote to `open` | V3 scans every 0.25s (better); entry usually promotes before handoff |

### Proposed F-8 — Enforce 4-step lifecycle in V3

**Goal:** Make the step sequence explicit and match V2 ordering, without adding user-visible delay.

**Changes:**

1. **Reorder `_scan_open_slot`** to mirror V2 `_poll_once`:
   ```text
   ensure stop (if not current)  →  then  breach check (if armed)
   ```
   Never enqueue breach handler before `stop_is_current()` is true.

2. **Breach arm gate** — only run phase breach logic when:
   - `stop_is_current(state)` is true, AND
   - `breach_watch.status` not in (`no_prices`, `stale`), AND
   - `not close_only_mode`

3. **Optional JSON field** `lifecycle.breach_armed_at` (ISO timestamp) set on first tick where all gates pass — aids dashboard and post-session audit.

4. **Log line** when breach arms:
   ```text
   Breach armed 11-00 P: stop=481142920 spread_mid=… threshold=…
   ```

**Expected timing after fixes (same as Jul 6 steps 1–3):**

| Step | Expected duration |
|------|-------------------|
| 1 → 2 (place → fill) | 0.2–2s (market / limit aggressiveness) |
| 2 → 3 (fill → stop) | 0.2–1s (one broker POST) |
| 3 → 4 (stop → breach armed) | 0.5–2s (MQTT subscription catch-up) |
| **Total** | **~1–5s** typical; well within “a few seconds” |

No intentional sleeps between steps except entry worker’s `FILL_WAIT` poll interval while **waiting** for a fill that has not arrived yet.

### V2 vs V3 lifecycle summary

| Step | V2 | V3 today | V3 after F-3/F-4/F-8 |
|------|----|-----------|-----------------------|
| 1 Place | Entry worker | Entry worker | Same |
| 2 Fill | Entry polls + backup sync | Entry polls + backup sync (10s throttle) | Same |
| 3 Stop | Before breach in `_poll_once` | `_on_load` usually; breach may race | Stop required before breach |
| 4 Breach | `execute()` with stop+MQTT gates | False enqueue on `open` | `execute()` gates only |

**Recommendation:** Use **V2** for live until F-3, F-4, and F-8 ship. The four-step model is sound; V3 broke step 4 only.

---

## Relationship to prior review items

| Prior item | This incident |
|------------|----------------|
| **F-1** (restart manual-kill resume) | Necessary for real manual kills; **combined with false breach → catastrophic** when applied to `breach_*` handlers live |
| **T-5** (breach restart recovery) | Now urgent — implement as part of F-4/F-6, not merely “observe” |
| **C1** (software breach live validation) | Jul 6 event was **anti-validation** — V3 never reached real C1; it false-triggered C3-style manual kill |

---

## Document history

| Date | Change |
|------|--------|
| 2026-07-06 | Initial incident write-up and fix proposals from live 11-00 IC session |
| 2026-07-06 | Added four-step lifecycle analysis (place → fill → stop → breach armed) and F-8 |
