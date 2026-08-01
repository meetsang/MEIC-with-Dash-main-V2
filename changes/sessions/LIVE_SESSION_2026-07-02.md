# Live Session Notes — Jul 2, 2026

**Status:** Operator log + slippage definition updated. **Restart dashboard** after deploy.  
**Related:** [LIVE_SESSION_2026-07-01.md](LIVE_SESSION_2026-07-01.md), [LIVE_SESSION_2026-06-26.md](LIVE_SESSION_2026-06-26.md)

---

## Observation — 11-00 PCS breached; slippage definition corrected

### What the operator saw

**11-00 PUT** (7420/7395 PCS) breached and closed via **exchange stop**:

| Item | Value |
|------|-------|
| Net credit collected | **$1.00** |
| Theoretical set stop (2× net credit) | **$2.00** spread exit debit |
| Brokerage spread exit | **$2.15** (short BTC $3.40 − long STC $1.25) |
| **Operator slippage** | **−$0.15/sp (−$15)** — paid more than the theoretical stop |

Previously the dashboard showed **+$20** because it measured **execution efficiency** (fills vs working limit orders), not theoretical stop vs brokerage.

### Fix shipped

**Slippage** now means: **theoretical set stop (stop× net credit) − brokerage spread exit debit**.

| Sign | Meaning |
|------|---------|
| **Positive** | Exited at or better than theoretical stop (lower debit = good for PnL) |
| **Negative** | Paid more than theoretical stop (bad for PnL) |

Only computed for **stop-out** closes (`exchange_stop`, `software_breach`, etc.). Manual / EOD closes show no slippage.

**Execution efficiency** (`exit_slippage`, leg slippage fields) is unchanged — still tracks fills vs order prices sent.

| File | Change |
|------|--------|
| `blocks/stop/stop_math.py` | `theoretical_stop_spread_debit()` |
| `blocks/stop/close_fills.py` | `stop_out_slippage_per_spread()` → `slippage_per_spread()` / dashboard |
| `dashboard/templates/index.html` | Label + column tooltip |
| `tests/test_close_fills.py` | Jul 2 PCS case: **−$15** |

**Operator:** Restart **dashboard** for label update. Existing closed trades recompute slippage on next dashboard load (not from stale JSON `exit_slippage`).

### Trade facts (from `11-00_P_20260702T105905.json`)

| Field | Value |
|-------|-------|
| Entry | **10:59 CT** — order 480714916, **$1.00** net credit |
| Short leg fill | **$1.87** (7420P) |
| Long leg fill | **$0.87** (7395P) |
| Exchange stop placed | **10:59** — order 480715036, stop **$3.50**, limit **$3.60** |
| Stop filled | **11:35 CT** — SPX ~7454 |
| Short BTC fill | **$3.40** |
| Long STC fill | **$1.25** |
| Spread PnL | **−$1.15/sp** (−$115) |

**11-00 CALL** (7520/7545 CCS) filled same tranche at **$1.00** credit; monitor separately.

### Two stop layers (still separate)

| Concept | Basis | This PCS |
|---------|-------|----------|
| **Theoretical set stop (slippage)** | 2× **net credit** | **$2.00** |
| **Software breach watch** | 2× net credit + $0.20 | **$2.20** |
| **Exchange stop (broker)** | 2× **(short fill − $0.10)** | **$3.50** short-leg trigger |

Slippage uses the first row. The exchange stop on the short leg is a separate broker backstop.

### Timeline

| Time (CT) | Event |
|-----------|--------|
| **08:27** | Launcher start |
| **10:59** | 11-00 PCS + CCS entries; PCS stop @ 3.50 / 3.60 |
| **11:35** | Exchange stop filled; long STC **1.25** — trade `closed` |

---

## Observation — Kill Selected on 3 MEIC CCS (~14:40 CT); closes felt sequential

### What the operator saw

Around **2:40 PM CT** on Jul 2, selected **three MEIC call credit spreads (CCS)** on the tranche grid and clicked **Kill Selected**. Expected behavior (by analogy with stop/breach handling): each trade’s monitor would kick off close work in parallel and the main loops would keep scanning other spreads. Instead, the three kills **felt sequential** — one trade appeared to finish (or move to closing) before the next progressed.

### Trades affected (from trade JSON)

Four MEIC **call** legs show `close_mechanism: manual_close` with stop cancels in the same ~3s window (operator reported **3**; JSON shows **four** CCS tranches — may include an extra selected row or a fourth checkbox).

| Tranche | File | Stop cancelled (CT) | Closed (CT) | Cancel → close |
|---------|------|---------------------|-------------|----------------|
| **02-00_C** | `02-00_C_20260702T135905.json` | 14:40:14 | 14:41:06 | **52s** |
| **01-15_C** | `01-15_C_20260702T131400.json` | 14:40:15 | 14:41:07 | **52s** |
| **01-45_C** | `01-45_C_20260702T134404.json` | 14:40:15 | 14:42:17 | **122s** |
| **12-00_C** | `12-00_C_20260702T115900.json` | 14:40:17 | 14:42:16 | **119s** |

All four have `stop_history` entry `spread_close_cancel:manual_close` (exchange stop cancelled before spread close). **12-30_C** (same day) closed via **exchange_stop**, not this kill batch. **11-00_C** remained open.

**Data note:** All four JSONs show `short_close_price: 0.20` and `long_close_price: null` at finalize — spread-close fill leg parsing did not persist the long STC fill (broker status may not have returned per-leg prices). PnL still computed via inferred long from open fill where needed.

### Timeline (reconstructed)

| Time (CT) | Event |
|-----------|--------|
| **~14:40:00** | Operator Kill Selected (estimated from stop-cancel timestamps + poll latency) |
| **14:40:10–13** | Last `breach_watch` updates on all four CCS — monitors alive |
| **14:40:14–17** | Exchange stops cancelled on all four (within **3 seconds**) |
| **14:41:06–07** | **02-00_C** and **01-15_C** marked `closed` |
| **14:42:16–17** | **12-00_C** and **01-45_C** marked `closed` |

**Two-wave pattern:** first pair ~**52s** after stop cancel; second pair ~**2 minutes**. Total wall time from first cancel to last close ≈ **2 min 3 sec**.

Stop-monitor process logs for this window were **not** retained in `logs/` (launcher log only shows subprocess restarts). Evidence is from trade JSON timestamps only.

### How Kill Selected works (design)

**Dashboard (fire-and-forget commands):**

1. `killSelected()` in `dashboard/templates/index.html` loops selected rows and **`await fetch('/api/close_trade')` sequentially** — one HTTP POST per trade. Each call only writes a small command file; this adds milliseconds, not minutes.
2. `POST /api/close_trade` → `trades/commands/{filename}.close.json` with `close_mechanism: manual_close`.

**Stop monitor (per-trade thread):**

1. `MonitorRunner` runs **one `StopMonitor` thread per open trade JSON** (`blocks/stop/runner.py`).
2. Each thread polls every **~3s** (`FAST_INTERVAL`).
3. On poll, `_check_dashboard_commands()` sees the `.close.json` file and calls **`replace_with_spread_close()` inline** in `_poll_once()` — same thread, **not** a background worker.

~~~ I thought the stop monitor does this in round robin and keeps a tab on all stops at brokerage one after another and only on a breach/fill condition it spins out a thread for taking care of subsequent steps for either replacing the stop with limit order and/or closing the long side. Is that not true?

**Reconciliation — your model vs what ships today:**

**Partially aligned on breach/fill follow-up, not on round-robin.**

| Your mental model | Actual implementation (`blocks/stop/runner.py`, `monitor.py`) |
|-------------------|---------------------------------------------------------------|
| **One** loop round-robins all trades and hits the brokerage for each stop in sequence | **One `StopMonitor` thread per open trade JSON** — each thread runs its **own** ~3s poll loop independently. There is no single round-robin pass over all stops. |
| Breach/fill only then spawns a thread for cancel + limit / long close | **Yes for breach and exchange-stop long chase:** `phase.execute()` → `_threaded_phase_execute`; long chase → `_threaded_long_chase`. **No for Kill Selected:** `replace_with_spread_close()` runs **inside** that trade’s poll thread (no spin-out). |
| Fast loop keeps scanning other spreads while slow work runs | **Per trade:** after breach, *that* trade’s poll loop returns quickly (`_breach_active` gate) so the same thread can keep doing MQTT breach checks. **Other trades** are on **other threads** anyway — they were never blocked by one trade’s breach handler. **Kill Selected** blocks *its* thread during cancel + place (up to ~30s on stop cancel confirm). |

~~~so does that thread get closed out once it takes care of breach/fill condition as it won't be needed for watching. Also do we have 6*12 MEIC and similarly additional monitor threads for manual trades running if none of the stops breach/fill? Isn't round-robin a better way to do this, the 3 sec delay means we are running each thread idling more than work, with round-robin that 3s delay will mean other stops getting serviced. 

**Reconciliation — thread lifecycle, count, round-robin:**

**Does the monitor thread exit after breach/fill?**

| Phase | Thread behavior |
|-------|-----------------|
| **Breach detected** | Main poll loop **keeps running** on that trade — sets `_breach_active`, spawns helper for broker work, then continues MQTT breach checks until status changes. |
| **`status: closing`** (stop filled or spread close working) | Same thread **still runs** — polls spread close or schedules long chase; breach watch is mostly irrelevant but the thread is not torn down yet. |
| **`status: closed`** | `monitor.run()` **breaks** out of its loop (`monitor.py` ~128–129). Thread **ends**. `MonitorRunner._supervise()` drops the handle when the thread is dead and status is `closed` / `cancelled`. |

So: the thread **does** go away once the trade is fully closed, not immediately when breach handling starts. There is no separate “watcher thread” that outlives the close.

**How many threads? Not 6×12 by default.**

`MonitorRunner.add()` only starts a monitor when **`status == open`** and the spread is **fully filled** (`runner.py` ~133–137). Empty / pending / paused / skipped session slots have **no** JSON or no open status → **no thread**.

On a typical day with ~6 lots × 2 sides entered, expect **~0–12 MEIC threads** (only legs that actually filled and are open), plus **one per open manual spread** — not 72 idle loops. Jul 2 at 14:40 had on the order of **~10 open MEIC + manual** monitors, not the full grid.

**Is round-robin better?**

**Your intuition matches GAP-22 discussion** — one supervisor cycling all open spreads every ~3s would:

- Do MQTT breach math on all legs in **<1 ms** total (no N× sleep)
- Use **one** broker slow-sync cadence instead of N threads each hitting REST on their own 10s timer
- Avoid N threads mostly **sleeping** 3s between polls

**Why it wasn’t built that way yet:** V1 carried forward **one thread per trade** for isolation and simpler recovery (Jun 26 ms-50 long-chase bug was per-file). GAP-22 added **background threads for breach response** as a middle ground, not full round-robin consolidation.

**Trade-off:** Round-robin is **more efficient at idle** and matches your “service all stops within 3s” mental model. Per-trade threads are **simpler to reason about** when one file misbehaves but **wasteful** when many legs are open and quiet — agreed as a future refactor candidate, separate from the Kill Selected spin-out fix.

**Where the round-robin idea comes from:** `changes/GAP_ANALYSIS.md` (GAP-22) *discussed* consolidating fast breach detection into one supervisor thread cycling all spreads (~3s cadence). That was the **design direction** for trending-market pileups. **What shipped** is still **N parallel monitor threads** (one per file), with **background threads only for breach response and long chase** — not a single round-robin broker poller.

**Why Jul 2 still felt sequential despite N threads:** all threads share **one** TastyTrade broker asyncio loop, so cancel/place/status calls **queue** at the API layer. Plus manual kill does not use the breach spin-out pattern, so each trade’s thread sits in `_cancel_stop_and_confirm()` until its turn at the broker completes.

~~~I am not sure how stop monitor differs here isn't it calling the TastyTrade broker asyncio loop? So if all the trades I manually closed would they be closed faster if breached/stop filled?

**Reconciliation — same broker loop; different close path:**

**Yes — manual kill uses the same `TastyTradeBroker` / asyncio loop as breach and exchange stop.** Every monitor thread, whether breach helper or inline kill, ends up in `_run()` → single event loop. **Serialization at the API is shared** — that part of the doc applies equally to all paths.

**Would those four CCS have closed faster if they had breached instead of Kill Selected?**

**Unlikely faster overall — possibly worse.**

| Path | What happens at broker | Legs |
|------|------------------------|------|
| **Kill Selected (Change 2)** | Cancel exchange stop → **one debit spread close** (both legs) | Single vertical order — correct for operator flatten |
| **Software breach** | Cancel stop → **short-leg limit** (breach pipeline) → may reprice → then long work | More steps, more queue time |
| **Exchange stop filled** | Stop hits → short closed → **30s delay** → long chase (many single-leg orders) | Slowest; by design for “short already gone” |
~~~but since all of them are on independent threads long leg would be closed within 30s of the short fill.

**Reconciliation — parallel long chase after exchange stop:**

**Partially true on parallelism, not on timing.**

- **Parallel:** Yes — each trade has its **own** monitor thread. When the exchange stop fills on trade A/B/C/D, each thread independently sets `status: closing`, waits `long_close_delay_sec` (**30s**), then starts **`_threaded_long_chase`** in a background thread. Four stops filling around the same time → four long chases **can overlap** (unlike Jul 2 manual kill blocking inline during cancel).

- **Timing correction:** Long chase does **not** start *within* 30s of short fill — it starts **after** a **30-second break** (`stop_profile.long_close_delay_sec = 30`). Wall clock from short fill to long close ≈ **30s + chase duration** per trade, not ≤30s.

- **Broker still serializes:** Long chases each issue many single-leg STC orders (cancel/reprice). All go through the **same** Tasty asyncio loop — overlap helps thread scheduling, not API throughput.

- **Vs Jul 2 manual kill:** Exchange-stop path needs the **market to hit the stop first** (not operator-initiated at 14:40). If all four stops had filled at once, you’d wait **30s**, then parallel long chases — **more orders and steps** than one spread close per kill. Manual kill was still the **right operator action**; the gap was missing spin-out + broker queue, not choosing the wrong exit product.

- **Where your point lands:** If comparing **parallelism feel** only, exchange-stop → background long chase **does** match your independent-thread model better than inline `replace_with_spread_close()`. That supports the **`_threaded_spread_close`** fix, not switching kills to wait for exchange stops.

So manual kill chose the **right** close shape (one spread). Jul 2 slowness was **not** because kill used the wrong product — it was **(a)** inline blocking on each thread during stop cancel confirm, **(b)** broker queue draining four cancels/places/fills serially, **(c)** working spread orders taking ~1–2 min to fill.

**Where breach *would* feel faster:** the poll loop **returns immediately** after spawning `_threaded_phase_execute`, so that thread isn’t stuck in a 30s cancel loop — **other trades’ threads** can still submit broker work. That helps **overlap**, not raw Tasty throughput. Kill Selected didn’t get that overlap.

**Net:** Same broker bottleneck; breach path doesn’t bypass it. Thread spin-out + (future) `_threaded_spread_close` would narrow the gap with breach; switching those kills to breach would **not** have been an improvement.

**Bottom line:** Breach/fill → background thread **is true**. Round-robin over all stops in **one** loop **is not** how the code works today — it’s **parallel per-trade loops**, with broker I/O effectively serialized.

**Contrast with breach / exchange-stop path (why operator expectation differs):**

| Path | Detection | Broker work |
|------|-----------|-------------|
| **Software breach** | Fast poll (MQTT only) | `phase.execute()` runs in **`_threaded_phase_execute` background thread** — poll loop returns immediately (`monitor.py` ~530–540) |
| **Exchange stop filled → long chase** | Poll detects `status: closing` | Long chase in **`_threaded_long_chase` background thread** (~496–501) |
| **Kill Selected / manual_close** | Poll sees command file | **`replace_with_spread_close()` runs synchronously** in the poll thread (~452–453) |

So breach/stop-fill paths **offload blocking broker I/O** to helper threads; **manual kill does not**.

### Why it felt sequential (root causes)

**1. Manual close is synchronous per monitor thread**

`replace_with_spread_close()` blocks its monitor thread for:

- `_cancel_stop_and_confirm()` — poll broker up to **30s** (0.5s steps) until stop leaves the book
- `place_spread_close_order()` — build legs + submit debit spread close
- On working fill: subsequent polls via `_poll_spread_close()` until filled

Unlike breach, there is **no** `_threaded_*` wrapper for manual kill.

**2. Single shared TastyTrade broker event loop**

All monitor threads share **one** `TastyTradeBroker` instance (`blocks/stop/run.py` → `get_broker()`). API calls use `asyncio.run_coroutine_threadsafe` + `future.result()` on a **single background event loop** (`tastytrade_broker.py` ~231–234). Coroutines run **one at a time** — parallel threads **queue** at the broker layer even when four monitor threads are active.

**3. Command pickup stagger (~0–3s per trade)**

Each monitor only reads command files on its **next poll** (up to 3s apart). Stop cancels still landed within 3s (14:40:14–17), so detection was reasonably parallel; **close completion** spread over ~70s between waves.

**4. Spread-close working time**

After stop cancel, each trade holds a **working debit spread** until fill. `_poll_spread_close()` blocks that thread’s poll loop while the order is working (returns `True`). Fill latency differs by strike/liquidity — contributes to staggered `closed` timestamps.

**5. Dashboard `await` loop (minor)**

Sequential `fetch` in `killSelected()` is **not** the main bottleneck; command files are written in quick succession.

### Comparison to design intent (GAP-22)

`changes/GAP_ANALYSIS.md` documents **GAP-22**: fast breach detection on the poll loop, **parallel threads for breach response** so multiple spreads breaching together do not pile up. That pattern **does not extend to Kill Selected** — manual close was implemented as Change 2 (`replace_with_spread_close`) but runs **inside** the poll path, not in a detached thread.

### Secondary finding (fill recording)

All four kills finalized with **`long_close_price: null`**. Spread close likely filled at the broker, but `_apply_spread_close_fill()` / `get_order_status` did not capture long-leg fill on the close order. Worth a separate fix; does not explain sequential *feel* but affects exit labels and slippage inference.

### Possible improvements (document only — no code change this pass)

1. Run `replace_with_spread_close()` in a **background thread** (mirror `_threaded_phase_execute`) so each monitor’s fast poll loop stays responsive.
~~~ I like this.

**Reconciliation:** Agreed as the **preferred fix** for Kill Selected parallelism — same pattern as breach: poll loop detects the command file, sets a flag, starts `_threaded_spread_close(reason)`, returns. Keeps MQTT breach checks and command pickup on cadence while cancel + spread place runs off-thread. Still subject to shared broker queue, but avoids one trade holding its poll thread for the full 30s cancel-confirm window.

~~~this needs little bit more talk. I think the same stop monitor knowing something is asked for manual close/kill, should stop the monitor thread and then take all the action or something similar.

**Reconciliation — “stop watching, then close” (expanded):**

Agreed on the **intent**: once Kill Selected is accepted for a trade, **breach/stop watching is over** — the operator wants flatten, not continued MQTT breach logic or exchange-stop babysitting.

**Suggested behavior (design target, not implemented yet):**

1. **Detect** `.close.json` (or killswitch) on poll — same as today.
2. **Immediately transition role:** set `close_mechanism`, `status: closing` (or a dedicated `closing_manual` flag), **disable breach phases** for this file (`_breach_active` / skip phase loop).
3. **Stop “watch” work on the fast path** — no more software-breach evaluation; optional: unregister exchange stop from alert listener since we’re cancelling it anyway.
4. **Run close to completion** in one dedicated flow (background thread or same thread, but **not** interleaved with 3s breach polls):
   - cancel stop + confirm
   - place spread close
   - poll until filled (or retry)
   - finalize + `move_to_closed`
5. **Exit** — `monitor.run()` breaks when `status: closed`; thread ends; runner drops handle.

**Two implementation options (equivalent operator outcome):**

| Option | Mechanism |
|--------|-----------|
| **A — Spin-out (item 1 above)** | Poll loop stays alive but **only** runs close polling (`_poll_spread_close`); breach loop skipped. Thread dies when closed. |
| **B — One-shot close worker** | Poll loop sets flag and starts `_threaded_spread_close`; **or** hands off to a short-lived worker that owns the JSON until `closed`, then main monitor thread exits early. |

Your wording (“stop the monitor thread and take all action”) maps closest to **B** or to **A with breach disabled** — either way the key is: **after kill, no more watcher duty**, only close execution until done.

**Why not literally kill the OS thread mid-poll today:** the same thread owns in-memory `self.state` and broker sequencing; exiting without finishing would orphan a working spread order. We **stop the watching behavior**, not abort mid-close.

**Pair with item 1:** `_threaded_spread_close` + `_manual_close_active` gate in `_poll_once()` (skip breach/phase block when set) delivers your model without rewriting the whole runner to round-robin.

~~~ Actually this could be the job of the stop monitor itself. There are three conditions it can be looking for instead of two as of today and take appropriate action. This design will make it easy:
1. Breach of stop - Replace the stop with limit on short leg, once filled chase long leg after 30 sec break.
2. Fill on Stop Order -  chase long leg after 30 sec break
3. Manual Close/Kill - Cancel the stop on short leg, chase the whole spread for the current price from mqtt.

**Reconciliation — unified three-condition monitor (operator design):**

**Agreed — this is the cleaner mental model and matches where the code should converge.**

Today these three paths **exist** but are **wired inconsistently** (breach/long chase spin out; manual kill runs inline; detection scattered across phase loop, alert fill queue, and command files):

| # | Condition | Operator intent | Today (`monitor.py`) | Target |
|---|-----------|-----------------|----------------------|--------|
| **1** | **Software breach** (MQTT spread ≥ threshold) | Cancel exchange stop → short **limit** → on short fill, **30s break** → long chase | `phase.execute()` in `_threaded_phase_execute` | Keep; already closest to spec |
| **2** | **Exchange stop filled** | Short already gone at broker → **30s break** → long chase | `handle_stop_order_update` → `closing` → `_threaded_long_chase` after delay | Keep; already per-thread parallel |
| **3** | **Manual close / kill** (dashboard command or killswitch) | Cancel exchange stop → **one spread debit close** priced from **MQTT** (both legs) | `replace_with_spread_close()` **inline** in poll thread | **Align with 1/2:** same monitor owns the file; on detect → disable breach watch → spin out close worker (or close-only poll mode) until `closed` |

**Key unify principles (from your comment):**

- **One monitor per open trade** watches for **three exit triggers**, not two — manual kill is a first-class condition, not a bolt-on command check.
- **Path 3 is not leg-by-leg** — “chase whole spread at MQTT” = existing `replace_with_spread_close()` (debit from short/long mids + tick adjust). Correct shape; needs threading + stop-watching off.
- **Paths 1 & 2** stay leg-by-leg **only because the short is already closed** (by limit or exchange stop) before long work begins — by design, not an operator-kill shortcut.

**Implementation sketch (doc only):** refactor `_poll_once()` into explicit state / trigger dispatch: `(breach \| stop_filled \| manual_close)` → handler. Handlers 1–2 unchanged; handler 3 gets `_threaded_spread_close` + breach skip. Optional later: round-robin **supervisor** that calls the same three handlers per file (GAP-22), without changing handler logic.

2. Optionally batch or parallelize dashboard close commands (minor).
3. Consider broker request queue with explicit concurrency limit vs. accidental serialization on one asyncio loop.
4. Improve spread-close fill parsing so both legs persist on manual kill.
~~~what?

**Reconciliation — item 4 in plain language:**

When the **vertical spread close order filled** at Tasty, all four Jul 2 kill JSONs saved **`short_close_price: 0.20`** but left **`long_close_price: null`**. The dashboard exit column then can’t show a proper `credit (short-long)` pair; PnL falls back to **inferring** the long exit from the **open** long fill (`close_fills.py`), which is wrong for display and skews any spread-level math.
~~~null=0, we need to assume that

**Reconciliation — missing long close price:**

**Agreed — operator rule: `long_close_price` null → treat as `0.00`, not inherit open long fill.**

| Approach | Jul 2 example (01-15_C) | Problem |
|----------|-------------------------|---------|
| **Current code** (`_resolved_long_close_price`) | Missing long → use open long **$0.37** | Exit looks like `0.20−0.37` (credit); misstates spread exit |
| **Your rule (null = 0)** | Missing long → **$0.00** | Exit debit ≈ **$0.20** (short BTC only); matches “long worthless at close” assumption for 0DTE CCS far OTM |

**Action when we fix kill/fill recording:** (1) persist actual long STC from broker when available; (2) until then, dashboard/PnL use **`0.0`** for null long close on closed manual/spread-close trades — **remove** open-fill inference in `_resolved_long_close_price` for display paths.

**Still worth fixing broker leg parsing** so we store the real STC when Tasty returns it — null=0 is the conservative display default, not a substitute for accurate fills.

### Files referenced

| File | Role |
|------|------|
| `dashboard/templates/index.html` | `killSelected()` sequential `fetch` |
| `dashboard/server.py` | `POST /api/close_trade` → command file |
| `blocks/stop/monitor.py` | `_check_dashboard_commands`, `replace_with_spread_close`, breach threading |
| `blocks/stop/runner.py` | One thread per trade |
| `brokers/tastytrade_broker.py` | Single-event-loop `_run()` |
| `changes/GAP_ANALYSIS.md` | GAP-22 breach parallel response |
| `changes/LIVE_SESSION_2026-06-26.md` | Prior spread-kill / long-chase incident |
