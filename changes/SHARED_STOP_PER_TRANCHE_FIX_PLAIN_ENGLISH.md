# Shared Stops on Same Strike — Fix Plan (Plain English)

**For:** Operator review before code change  
**Date:** 2026-07-07  
**Operator decisions recorded:** 2026-07-07 (evening)  
**Related:** [LIVE_SESSION_2026-07-07.md](LIVE_SESSION_2026-07-07.md), [STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md](STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md)

This document explains **what went wrong on Jul 7**, **what we want instead**, and **how we plan to fix it** — without requiring you to read Python.

---

## Part 1 — The problem in one sentence

When two or more MEIC tranches use the **same short strike** (e.g. three lots of 7485P), the bot **reused one brokerage stop** for all of them instead of placing **one stop per tranche**.

That left most tranches **unprotected at the exchange**, and when software breach fired, **all tranches fought over the same order**.

---

## Part 2 — What happened today (7485P example)

| Time (CT) | What happened |
|-----------|----------------|
| **11:59** | **12-00_P** (first tranche) places real exchange stop **`481561791`** on 7485P |
| **12:29** | **12-30_P** fills on the same strike. Slow sync **adopts** stop `481561791` — **no new stop placed** |
| **13:44** | **01-45_P** fills. Same thing — adopts `481561791` |
| **13:49** | Market moves. Software breach fires on 01-45_P and 12-30_P |
| **13:49** | Breach **cancels** the one shared stop, then places short-leg limits. Tranches **step on each other** (cancel/reprice/retry) |
| **After** | **12-00_P** briefly had **no** exchange stop until the bot replaced it (~13:49:41) |

**Operator impact:** You thought each tranche had its own stop. In reality, Tasty had **one** stop for **one** lot, and JSON for three tranches all pointed at it.

---

## Part 3 — Why the code does this (root cause)

Two pieces of logic work together:

### A. “Reconcile with broker” on every slow sync

Roughly every few seconds, the stop monitor asks Tasty: *“Is there already a working buy-to-close on this short symbol?”*

If yes, it **writes that order ID into this tranche’s JSON** and logs `Reconciled active stop … → order 481561791`.

That was meant for **manual repair** (operator placed a stop by hand). It was **not** meant to run on every tranche at fill time.

**Code:** `blocks/stop/monitor.py` → `_reconcile_active_stop_with_broker()`  
**Irony:** `blocks/stop/broker_sync.py` says adopt-from-broker is *“manual repair only — each tranche places its own stop”* — but production slow sync does the opposite.

### B. “Stop is current” only looks inside one JSON file

Before placing a stop, the bot checks: *“Does **this** JSON already have enough stop quantity recorded?”*

After reconcile, each JSON says it “owns” the shared stop with enough qty for **that** tranche. The check passes → **`setup_initial_stop()` never runs**.

**Note:** Tranche quantity is often **1**, but it can be changed via the dashboard or internal config. The bug is the same at any qty: reconcile makes each JSON look fully stopped when only **one** broker order exists.

**Code:** `blocks/stop/fill_sync.py` → `stop_is_current()`  
**Called from:** `_ensure_stop_for_filled_qty()` in `monitor.py`, and V3 supervisor scan.

---

## Part 4 — What we want (operator rules)

Think of **three different actions**. They must **not** share the same “look for existing orders” rule.

| Action | Should we look for existing broker orders? | Why |
|--------|--------------------------------------------|-----|
| **Place exchange stop** (new tranche) | **No** | Each tranche is its own vertical (1 or more lots). Each needs its **own** stop for **its** qty. |
| **Close spread** (breach, manual, phase 3) | **Only inside this trade’s JSON** | Prevent duplicate close on the *same* tranche (Jul 6 lesson). |
| **Close across tranches** | **No** | Tranche B must not adopt or block on tranche A’s close order. |

**Analogy:** Stops are like **seat belts** — one per passenger. Close-order dedup is like **don’t ring the fire alarm twice for the same room** — not *don’t ring it because the next room already rang theirs*.

**Same-strike, multiple tranches all stopping at once:** Even if every tranche on 7485P hits its stop in the same minute, each tranche still closes **on its own JSON** — no cross-tranche “already closing” skip.

---

## Part 5 — The fix plan (plain English)

### Fix 1 — Stop placement: always place per tranche (P0)

**What we will change**

- Remove **adopt-any-working-stop** from production slow sync (`_reconcile_active_stop_with_broker`).
- When a tranche is filled and has no working stop **in its own JSON**, call **`place_stop_order`** for that tranche’s qty — **even if** Tasty already shows another stop on the same symbol from an earlier tranche.

**What we will keep**

- If **this JSON’s** `active_stop.order_id` is still working at Tasty → refresh status only (no new place).
- If **this JSON’s** stop was cancelled/rejected → replace **this tranche’s** stop only.

**Optional narrow reconcile (manual repair only)**

- Adopt a broker stop **only** when:
  - This JSON has **no** `active_stop.order_id`, **and**
  - Operator ran an explicit repair script / adhoc command, **not** the normal fill path.

We will **not** use `find_working_close_order(short_sym)` during normal stop placement — that function returns the **first** BTC on the symbol, which is another tranche’s stop.

**Files (expected):** `blocks/stop/monitor.py`, `blocks/stop/v3/supervisor.py`, possibly gate `broker_sync.adopt_active_stop_from_broker()` to CLI-only.

---

### Fix 2 — Close placement: within-trade gate only (P0)

**What we will change**

- When deciding whether to send a **spread close** or **breach short limit**, check **only this trade JSON**:
  - `spread_close_order_id` already set?
  - `close_only_mode` already true?
  - `active_stop` on **this** JSON already a working LIMIT close?
- Do **not** skip placing a close because **another tranche’s** close order exists on the same symbol — including when several same-strike tranches all stop together.

**Breach / stop cancel scope (operator decision — see Part 8)**

- Cancel **only** the stop recorded on **this** JSON’s `active_stop.order_id`.
- Do **not** cancel other tranches’ stops on the same short symbol, even if the strike matches.

**What stays the same (Jul 6 protections)**

- F-5 / F-6: don’t close twice on the **same** JSON when already flat or `spread_close_order_id` present.
- F-9 preflight: account flat / mismatch before manual close.

**Files (expected):** `blocks/stop/monitor.py`, `blocks/stop/v3/handlers/manual_kill.py`, `blocks/stop/v3/handlers/software_breach.py`, `broker_sync.cancel_all_close_orders_on_short` — replace broad “cancel all BTC on symbol” at breach with per-JSON cancel where used for exit.

---

### Fix 3 — Optional safety net: qty audit (P1, recovery only)

**When to use:** Adhoc repair or recovery after a glitch — bot crashed, JSON lost `order_id`, or a tranche has fills but **no** stop order on file.

**What it does:** Compare:

- Broker short qty on symbol (e.g. 3 lots 7485P short), vs
- Sum of `stop_quantity` across **open** JSONs on that symbol (e.g. 1+1+1)

If broker stops **<** lots held → log **alert** and optionally place the missing stops.

**When not to use:** On every normal fill (adds latency and API calls). Not a substitute for Fix 1.

---

## Part 6 — Implementation order

| Step | Task | Risk if skipped |
|------|------|-----------------|
| 1 | Tests: two tranches same strike → **two** `place_stop_order` calls, **two** distinct order IDs in JSON | Regress back to shared stop |
| 2 | Disable cross-tranche reconcile in `_reconcile_active_stop_with_broker` | Same |
| 3 | Breach cancel scope: cancel **this JSON’s** `active_stop` only, not all BTC on symbol | Jul 7 cascade repeats |
| 4 | Within-trade close dedup audit (no cross-tranche skip) | Duplicate close on one JSON |
| 5 | Optional qty safety net + operator alert (adhoc / recovery) | Manual recovery only |

---

## Part 7 — How we will verify (test plan)

1. **Paper / sim:** Open two MEIC put tranches same strike 5 minutes apart → confirm **two** working stops at broker and two IDs in JSON.
2. **Replay Jul 7 JSON:** With fix, 12-30_P and 01-45_P would each get **new** stops, not reconcile to `481561791`.
3. **Breach:** Only the breached tranche’s stop cancelled; sibling tranche stops remain.
4. **Manual close:** Second click on same trade still blocked; **different** trade same strike still closable.
5. **Simultaneous stops:** Two same-strike tranches both exit — each closes on its own JSON, no cross-tranche close skip.

---

## Part 8 — Operator decisions (resolved)

| # | Decision | Operator answer |
|---|----------|-----------------|
| **Q1** | Breach: cancel all BTC on short symbol, or only this JSON’s `active_stop`? | **Only this JSON.** Same short strike can sit on tranches with **different entry prices** → different stop trigger prices and different times to stop. Let each tranche’s prices decide when it stops. **Do not cancel or close for other tranches.** |
| **Q2** | Same strike, different long strikes — still one stop per JSON? | **Yes** — same rule as Q1. |
| **Q3** | Qty safety net: dashboard health check vs adhoc? | **Adhoc or recovery** when a tranche has no stop order recorded (glitch / missing JSON state). Not on every fill. |

---

## Part 9 — Not in scope for this fix

- Software breach threshold (`2× + $0.20` vs raw `2×`) — separate change; see LIVE_SESSION breach tabled section.
- Breach execution price (short mid chase vs cap at 2× spread debit) — separate change.
- **Phase 2 stop upgrade** — no separate operator policy change expected; it should **inherit the same per-tranche rule** as Fix 1 once implemented (one upgrade per JSON, not shared across tranches).

---

## Appendix — Code map (for implementer)

| Area | File | Function |
|------|------|----------|
| Cross-tranche adopt | `blocks/stop/monitor.py` | `_reconcile_active_stop_with_broker()` |
| Skip place | `blocks/stop/monitor.py` | `_ensure_stop_for_filled_qty()` |
| Skip place (V3) | `blocks/stop/v3/supervisor.py` | `_scan_open_slot()` + `stop_is_current()` |
| Manual adopt (OK) | `blocks/stop/broker_sync.py` | `adopt_active_stop_from_broker()` |
| Find first BTC | `brokers/tastytrade_broker.py` | `find_working_close_order()` |
| Cancel all BTC (review) | `blocks/stop/broker_sync.py` | `cancel_all_close_orders_on_short()` |

---

*Status: **Implemented** (P0 live-safety, 2026-07-07).*
