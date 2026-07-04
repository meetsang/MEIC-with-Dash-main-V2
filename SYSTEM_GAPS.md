# MEIC System Gaps — TastyTrade / stop_monitor

> Living list of known gaps between intended MEIC behavior, the legacy Schwab path, and the current TastyTrade implementation. For validation steps and scenarios, see [TESTING.md](TESTING.md). For original port goals, see [MEIC_to_Tasty_Project_Plan.md](../MEIC_to_Tasty_Project_Plan.md).

---

## Purpose

This document tracks **behavioral and operational mismatches** that remain after porting MEIC from Schwab (`closetask.py`, `longclose.py`, shared `order_params.json`) to TastyTrade (`stop_monitor`, per-trade JSON under `trades/active/`). It is not a test plan — it is a prioritization aid for closing the gap without re-litigating resolved items each session.

---

## Priority legend


| Priority | Meaning                                                                                                 |
| -------- | ------------------------------------------------------------------------------------------------------- |
| **P0**   | Production risk — wrong fills, orphan legs, or silent failure under live money                          |
| **P1**   | Behavioral mismatch vs legacy or user mental model — may be acceptable short-term but should be tracked |
| **P2**   | Nice-to-have, ops convenience, or future architecture                                                   |


---

## Gaps

### Unified 5s poll couples breach checks with broker sync

**Priority:** P1

**Legacy / expectation:** Schwab `closetask.py` ran a **3s** breach/strike loop while stop **fill status** was checked roughly every **30s** (`count % 10` in the 3s loop). Users expect **fast** software breach reaction and **slower**, cheaper broker polling for order state.

**Today:** `StopMonitor._poll_once()` runs on a single **5s** interval (default `--poll 5`). Each cycle reads MQTT for breach/phase logic **and** calls `_sync_working_stop_order()` against the broker. Entry fill sync is throttled separately to **60s** (`FILL_SYNC_INTERVAL_SEC`).

**Risk if unfixed:** Breach detection is **~2s slower** than legacy (5s vs 3s) and tied to poll cadence; conversely, stop-status REST calls are **more frequent** than legacy (~5s vs ~30s), increasing API load without improving breach speed.

**Suggested direction:** Split into two loops or threads: a **fast MQTT path** (≤1s) for spread-mid breach, kill switch, and Phase 3 SPX proximity; a **slow broker path** (~30s) for entry fills and optional working-stop status. Keep JSON as the handshake between them.

~~~Lets elaborate it further I am thinking if I am not getting the whole picture. Lets groome it further.

---

### Entry fill sync cadence vs legacy fill check

**Priority:** P2 (partially addressed)

**Legacy / expectation:** Open-order fill awareness was polled on a ~30s cadence inside the close engine; project discussions also referenced ~5min-style throttling for some paths.

**Today:** Entry `open_order_id` sync is **60s** (`fill_sync.py`), with `force=True` on monitor load. This is **faster** than a 5min throttle and in line with the entry thread’s 60s `FILL_WAIT_MAX`.

**Risk if unfixed:** Low for production — partial fills may lag stop resize by up to one minute unless the monitor restarts (immediate sync on load).

**Suggested direction:** If decoupling polls, keep entry sync at **30–60s**; document that stop resize latency is bounded by that interval, not the 5s monitor tick.

~~~Lets elaborate it further I am thinking if I am not getting the whole picture. Lets groome it further. BTW I updated fill_wait_max to 5 from 60.

---

### Working stop polled every cycle, not every ~30s

**Priority:** P2

**Legacy / expectation:** Stop fill status ~**30s**.

**Today:** `_sync_working_stop_order()` runs **every 5s poll** — faster fill detection, more REST traffic.

**Risk if unfixed:** Rate limits or noise in logs; not a correctness bug given `get_order` fallback (see Resolved).

**Suggested direction:** Move stop-status sync to the slow broker path (~30s) once AlertListener re-registration is fixed or deemed unnecessary.
~~~Lets elaborate it further I am thinking if I am not getting the whole picture. Lets groome it further.
---

### AlertListener one-shot registration

**Priority:** P1

**Legacy / expectation:** Schwab fill checks were poll-based; no websocket equivalent. Real-time fill push is a **new** capability that should cover all active close orders.

**Today:** `MonitorRunner.add()` registers `AlertListener` **once** at thread start for `active_stop.order_id`. After **stop resize** (partial entry), **Phase 1 breach** (`replace_with_limit_close` → new LIMIT id), or **Phase 2 upgrade** (stop replace), the new order id is **not** re-registered. Fills for replaced orders rely on **REST poll** only.

**Risk if unfixed:** Stop fills may be detected up to one poll late (~5s); websocket push silently missed for replaced ids. Worst case: poll also fails briefly → delayed long-leg close.

**Suggested direction:** On any `active_stop.order_id` change, `unregister(old)` + `register(new)` and attach queue to the running `StopMonitor`. Alternatively, a single shared listener that routes by latest id from in-memory state.
~~~is there something to stream or real-time fill push thing? Lets groom this as well, be elaborative you are too technical.
---

### Phase 1 breach uses spread mid, not short mid vs exchange stop

**Priority:** P1 (document as intentional)

**Legacy / expectation:** Operators often think of breach as “short leg mid crossed the exchange stop price.” Legacy `shortclose.py` compared **spread cost** to a threshold derived from `two_x_short + offset`, not the resting stop trigger alone.

**Today:** Phase 1 fires when `spread_mid = short_mid − long_mid >= two_x_short + 0.20`. Exchange **STOP_LIMIT** (Mechanism A) is independent — it triggers on the short leg at the broker. Software breach (Mechanism B) cancels the exchange stop and places a **LIMIT** at live MQTT **short** mid.

**Risk if unfixed:** False sense of alignment — a short rally with long rally can move spread mid differently from short mid alone. Not necessarily wrong vs legacy math, but **confusing in ops** if conflated with the exchange stop price.

**Suggested direction:** Keep spread-mid formula; add dashboard/docs labeling “software breach threshold” vs “exchange stop trigger.” No code change required unless product owner wants short-only breach.
~~~ I need more info here, something is missing why are we subtracting short_mid and long_mid?
---

### Exchange stop vs software breach — two mechanisms

**Priority:** P2 (awareness)

**Legacy / expectation:** Same dual mechanism — resting stop plus software replace on spread breach.

**Today:** Implemented as two paths (see [TESTING.md — Scenario 3](TESTING.md)). Breach limit **reprices** on short MQTT mid each poll; exchange stop does not.

**Risk if unfixed:** Operators may cancel one mechanism thinking the other covers them.

**Suggested direction:** Operational checklist in dashboard or runbook; optional JSON field noting which mechanism closed the trade.

---

### Long leg close — no chase/replace loop

**Priority:** P1

**Legacy / expectation:** `longclose.py` **replaced** the long `SELL_TO_CLOSE` limit on a timer until filled.

**Today:** `_close_long_leg()` places **one** limit at MQTT long mid. No repricing if the market moves.

**Risk if unfixed:** Long leg may sit unfilled while JSON already shows `closed`; naked short exposure period extended vs legacy.

**Suggested direction:** Port long-close chase loop (timer + cancel/replace at streamed mid) similar to breach limit reprice on the short leg.

---

### Long close order not tracked; finalize does not wait for long fill

**Priority:** P0

**Legacy / expectation:** Close task waited for long fill before writing final close params.

**Today:** `handle_stop_order_update` → `_close_long_leg()` → `_finalize_close()` moves JSON to `trades/closed/` **immediately** after placing the long limit. Long order id is **not** stored in JSON; no poll for long fill completion.

**Risk if unfixed:** **Orphan long** or **orphan short** at broker while state file says closed; dashboard/SQLite history wrong; restart will not resume long close.

**Suggested?:** Track `long_close.order_id`, poll until filled/cancelled, only then finalize (or separate `closing` status).



~~~Lets track this and only after its close we can mark the json closed.

---

### Concentration risk on isolated long close tests

**Priority:** P1 (ops)

**Legacy / expectation:** Full exit chain closes short first, then long.

**Today:** `test-long-close` can place long `SELL_TO_CLOSE` while short **STOP_LIMIT** still working. Full JSON qty without `--quantity 1` may hit TastyTrade `margin_check_failed` / concentration rules.

**Risk if unfixed:** Failed tests mistaken for code bugs; accidental full long close with short still open in manual testing.

**Suggested direction:** Default adhoc to `--quantity 1`; document that full-chain tests must use `stop-fill-session` (cancels short stop first).

---

### Windows JSON file locks

**Priority:** P2

**Legacy / expectation:** Shared `order_params.json` had similar editor-lock issues.

**Today:** Per-trade JSON under `trades/active/`; retries and in-memory `trade_state()` reduce races. **Notepad++** or other editors holding an exclusive lock can still block writes.

**Risk if unfixed:** Stale on-disk state during manual inspection; rare in unattended production.

**Suggested direction:** Document “close JSON in editor while monitor runs”; optional read-only open hint in dashboard.

---

### MQTT events for dashboard (future)

**Priority:** P2

**Legacy / expectation:** Dashboard subscribed to MQTT for live prices.

**Today:** MQTT is **market data only**. Trade lifecycle is JSON + SQLite. No `MEIC/trade/{lot}/fill` events.

**Risk if unfixed:** Dashboard refresh latency only.

**Suggested direction:** Optional MQTT **events** for UI speed; **JSON remains source of truth** until multi-machine consumers require otherwise.

---

### Trade state filename / multi-tranche

**Priority:** P2 (mostly done)

**Legacy / expectation:** One row per lot/side in shared JSON — collisions on re-entry.

**Today:** One file per trade: `MEIC_IC_SPX_{yymmdd}_{lot}_{HHMM}_{side}_{orderTail6}.json`. `open_order_id` is the fill-sync key. Strike overlap guard ported (`strike_guard.py`).

**Remaining gap:** Operators must prune stale `trades/active/` files manually if a lot is abandoned; no automatic archival of expired pending JSON.

**Suggested direction:** Optional janitor for `pending_fill` files older than N days; dashboard warning on duplicate lot+side active files.

---

### Entry partial fill — short-before-long model (RESOLVED)

**Priority:** Was P0 — **fixed this cycle**

**Was:** Treating leg fills independently could stop the short before the long opened.

**Today:** `filled_quantity = min(short_filled, long_filled)` in broker + `fill_sync`; stop sized to **paired spread units** only. See [TESTING.md — Scenario 4](TESTING.md).

**Risk:** None if current code deployed.

**Suggested direction:** None — keep unit tests (`test_fill_sync.py`, `test_partial_fill_stop.py`).

---

### 3:00 PM admin close vs Phase 3 broker flatten

**Priority:** P1

**Legacy / expectation:** Hard 15:00 CT cutoff in `closetask`; Phase 3 (~2:51) actively **market-closes** short and long when SPX is near strike.

**Today:**


| Time (CT) | Behavior                                                                                                     |
| --------- | ------------------------------------------------------------------------------------------------------------ |
| ≥ 2:51    | **Phase 3** — cancel stop, market close short, `_close_long_leg()`, finalize                                 |
| ≥ 3:00    | **Admin close** — `_finalize_close('market_close_3pm')` only: JSON → `trades/closed/`, **no broker flatten** |


Integration mode (`MEIC_INTEGRATION=1`) skips the 3:00 PM admin path.

**Risk if unfixed:** After 3:00 PM, monitor stops watching but **broker positions and working stops may remain** unless Phase 3 already ran.

**Suggested direction:** Either invoke broker flatten at 3:00 PM admin close, or rename/document as “monitor shutdown only” and rely on Phase 3 + manual cleanup.

---

### No automated CI pass/fail on MQTT counts

**Priority:** P2

**Legacy / expectation:** N/A (manual Schwab verification).

**Today:** Integration sessions print MQTT totals; no CI gate on minimum message counts or `order_id` presence in `integration_report.json`.

**Risk if unfixed:** Streamer regressions slip through unit tests.

**Suggested direction:** Optional integration job with mocked broker + embedded Mosquitto; assert SPX and leg topics > 0 over a short window.

---

### seed-from-order adhoc not built

**Priority:** P2

**Legacy / expectation:** Manual seed fields (`--short-fill`, `--long-fill`, etc.) for existing positions.

**Today:** `seed-stop` requires explicit fills and strikes. No command that takes only `open_order_id` and hydrates JSON from TastyTrade.

**Risk if unfixed:** Slower ops when linking an existing broker position to the monitor.

**Suggested direction:** Adhoc command: fetch order by id → populate legs, credit, `filled_quantity`, write active JSON.

---

## Resolved recently

Fixes landed during the current test/integration cycle (see [TESTING.md](TESTING.md) for commands).


| Item                                   | Was                                          | Now                                                          |
| -------------------------------------- | -------------------------------------------- | ------------------------------------------------------------ |
| Stop fill after order leaves live book | `get_live_orders` only → missed filled stops | `get_order_status` falls back to `account.get_order(id)`     |
| Partial entry fill semantics           | Risk of stopping unpaired short              | Paired spread units via `min(short, long)` + resize stop     |
| `test-long-close` quantity             | CLI `--quantity` ignored                     | `--quantity` overrides JSON (use `1` for safe live test)     |
| Monitor load after partial Step 2      | Stop qty could lag new fills                 | `_on_load` resizes when `filled_quantity` > `stop_quantity`  |
| Pending JSON on startup                | Null `active_stop` edge cases                | `_on_load` syncs fills with `force=True`, adopts broker stop |


---

## Recommended sequencing

If implementing gaps, suggested order:

1. **P0 — Long close lifecycle** — Track long order id, wait for fill (or explicit `closing` state) before finalize. Unblocks trustworthy exit accounting.
2. **P1 — Decouple fast breach from slow broker sync** — Restores ≤3s-class breach reaction without increasing REST load; pairs naturally with throttling stop-status to ~30s.
3. **P1 — AlertListener re-registration** — Cheap win once order ids churn on resize/breach/Phase 2; reduces reliance on poll for fills.
4. **P1 — Long leg chase loop** — Parity with legacy `longclose.py` after lifecycle tracking exists.
5. **P1 — 3:00 PM admin close semantics** — Decide broker flatten vs monitor-only shutdown; document operator action if positions remain.
6. **P2 — seed-from-order, CI MQTT gates, JSON janitor, MQTT dashboard events** — Ops and hygiene after core exit/monitoring paths are solid.

---

*Last updated: Jun 2026 — align with [TESTING.md](TESTING.md) when behavior changes.*