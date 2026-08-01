# Stop Monitor V3 — Open Points Explained (Plain English)

**For:** Operator review before going live again  
**Date:** 2026-07-07  
**Companion to:** [STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md](STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md) (technical repair plan)

This document explains **what went wrong**, **what we need to fix**, and **every open question** in everyday language. No code required to read it.

---

## Part 1 — What happened on July 6 (the 11:00 trade)

### In one sentence

The bot opened your 11:00 iron condor correctly, then **mistakenly thought it needed to exit**, closed both sides, and then **tried to close again on an empty account** — which accidentally **opened new debit spreads** at Tasty.

### Step by step (what you would have seen)

1. **10:59** — Bot opens put spread and call spread. Exchange stops are placed. So far, normal.
2. **Within 5 seconds** — Bot decides to close both legs. **No stop was hit. No real breach. You did not click close.**
3. **First round of closes** — Put and call close correctly (you are flat).
4. **Second round of closes** — Bot tries to close again even though you already have **no position**.
5. **Tasty’s behavior** — When you send a “close” order but have nothing to close, Tasty can treat it as an **open** order instead. That created accidental debit spreads.

### This was NOT caused by

- Bad market data from Tasty
- A real stop fill
- You pressing kill or close
- The orphan process from July 4 (that was a separate housekeeping issue)

### This WAS caused by

A **logic mistake in V3** — the new stop monitor version. It confused two different ideas:

| Idea | What it should mean |
|------|---------------------|
| **“Watch this trade”** | Trade is open; keep an eye on it; make sure the stop is in place |
| **“Exit this trade now”** | Something real happened; cancel stop and send a closing order |

V3 treated **“trade is open”** as **“exit now.”** That is the core bug.

---

## Part 2 — The four steps every trade should follow

Think of each new trade like arming a security system:

```
Step 1 — Open the spread          (enter the trade)
Step 2 — Confirm it filled        (we really own it)
Step 3 — Place the exchange stop  (broker-side protection)
Step 4 — Turn on breach watching  (only NOW watch for software exit rules)
```

**July 6 failure:** V3 jumped to “exit” at Step 1, before Step 4 was even properly armed.

**After the fix:** The bot must complete Steps 1–3 and have live prices before it is allowed to evaluate breach rules or send any close.

---

## Part 3 — Every fix, explained simply

Below, “P0” means **must have before live**. “P1” means **strongly wanted** but less dangerous if delayed.

---

### F-3 — Don’t confuse “watching” with “exiting”

**The problem**  
V3 asks each “phase” (time period of the trade day): *Should I pay attention to this trade?*  
For Phase 1, the answer is always **yes** for any open trade. That is correct for **watching**.

But V3 then immediately started the **exit process** — as if a breach already happened.

**The fix**  
Each phase must answer a clearer question:

- **Nothing to do** — skip
- **Maintenance only** — e.g. upgrade the stop in Phase 2; do **not** close
- **Exit required** — a **real** rule fired (breach, time-based Phase 3 exit, etc.)

Only **“Exit required”** is allowed to start closing the trade.

**Your decision:** Use the cleaner “three-answer” design (called PhaseAction in the technical doc). Good choice for a full fix tonight.

**Plain analogy:** A smoke detector should not call the fire department just because you turned it on. It should call only when it actually smells smoke.

---

### F-4 — After a restart, don’t use the wrong recovery playbook

**The problem**  
If the bot crashes or restarts while a trade is in a weird state, V3 has a “recovery” path. That path was too simple: *If the trade looks like it’s exiting, resume manual close.*

But on July 6 the trade was marked “exiting” for the **wrong reason** (`breach_phase1_initial_stop` — a false alarm). Recovery still ran **manual close**, which cancelled stops and sent spread closes.

**The fix**  
Use a **decision table** — different situations get different responses:

| Situation | What recovery should do |
|-----------|-------------------------|
| Trade already closed | Do nothing |
| **You** clicked manual close | Resume that close (correct) |
| Admin killswitch | Resume that close (correct) |
| False breach flag, no close order yet | **Do not** manual close; re-check or wait |
| Breach close already sent | Poll that order; don’t send another |
| Unknown messy state | **Quarantine** — log it, don’t send orders |

**Plain analogy:** If your GPS says “recalculating” you don’t always make a U-turn. You need to know *why* it recalculated.

---

### F-8 — Enforce the four-step order (stop before breach watch)

**The problem**  
V3 could try to evaluate breach rules before the exchange stop was confirmed, or before live leg prices were available.

**The fix**  
Hard order every cycle for an open trade:

1. Is the trade fully filled? If not, wait.
2. Is the exchange stop placed and sized correctly? If not, fix that first.
3. Are live MQTT prices available and fresh? If not, wait — **do not exit**.
4. Only then mark “breach armed” and evaluate rules.

**Plain analogy:** Don’t evaluate whether to sell a house until the locks are installed and the alarm is actually on.

---

### F-5 — Never send a second close if the first one already worked

**The problem**  
After the first close filled, V3 **started the close process again** within a fraction of a second. That caused Round 2 orders on a flat account.

**The fix**  
Before any close order, check:

- Is the trade already marked closed? → Stop.
- Did we already get a fill on a close order? → Stop (or just poll that order).
- Does Tasty still show an open spread position? → If **flat**, do **not** send another close.

**Plain analogy:** Don’t lock the front door twice — and definitely don’t “lock” it when you’re already outside.

---

### F-6 — Clean up “exit in progress” flags after a successful close

**The problem**  
Even after a close fills, leftover flags like `close_only_mode` can make the bot think an exit is still active on the next scan.

**The fix**  
When a close completes:

- Mark trade **closed**
- Turn off “exit in progress” flags
- Save history for the audit trail (what happened, which orders) in a separate “archive” section
- Remove the trade from the active watch list

**Plain analogy:** When you finish checking out of a hotel, they deactivate your key card. The receipt stays, but the key shouldn’t open the door anymore.

---

### F-9 — Last safety net at Tasty: don’t send close if nothing to close

**The problem**  
Even with all the above, if a close order somehow gets through when you’re flat, **Tasty may open a new position** instead of closing.

**The fix**  
Right before any spread close hits Tasty’s API: **look at actual positions**. If flat or quantities don’t match → **block the order**. Log it. Do not transmit.

This is the **airbag** — last line of defense.

**Plain analogy:** The bank shouldn’t transfer money if your balance is zero, even if you accidentally click “withdraw” twice.

---

### F-7 — Better handling when prices aren’t ready yet (recommended)

**The problem**  
Right after entry, leg prices sometimes aren’t in MQTT yet. July 6 logs showed `missing MQTT`. That didn’t cause the false exit by itself, but it makes the first seconds confusing.

**The fix**  
If prices are missing or stale: **wait**, keep the exchange stop, don’t evaluate breach, don’t exit.

---

### F-10 — Fix the dashboard so PnL isn’t wrong after an incident

**The problem**  
After duplicate closes, the dashboard showed **garbled PnL** because it treated every fill as a normal close.

**The fix**  
Dashboard should:

- Use the **first valid close** for normal PnL
- Flag duplicate close attempts separately
- Not count accidental “cleanup” positions as normal strategy PnL

**Your decision:** Do this **before** V3 live tomorrow — agreed.

---

## Part 4 — Review items (R-1 through R-8) in plain English

These came from extra review after the main incident doc was written.

---

### R-1 — Only one stop monitor should run at a time

**What it is**  
The main launcher (`run.py`) starts one stop monitor. But if an old one was started manually days ago, **two can run at once**. That happened July 4 — an orphan ran until you killed it.

**Risk**  
Double API calls to Tasty, confusion, possible IP throttling. It did **not** cause the July 6 double-close (logs show one process that day).

**Your decision**  
Treat as one-off for now; use the check script before open (see R-2 / Q6).

---

### R-2 — Morning and end-of-day cleanup checklist

**What it is**  
A short routine: before starting the bot, confirm you have **0 or 1** stop-monitor process. After shutdown, confirm **0** and that the heartbeat file stops updating.

**Why it matters today**  
You killed the session right after the 11:00 incident — normal end-of-day cleanup may not have run.

**Your decision**  
Do this **tomorrow morning** before `run.py`.

**How (simple):**

```powershell
cd MEIC-with-Dash-main-V2
uv run python scripts/check_stop_monitor.py
```

- **Before open:** 0 processes is fine. After you start `run.py`, you should see **1**.
- **After shutdown:** should be **0** again.
- If you see **2 or more:** run `uv run python scripts/check_stop_monitor.py --kill`

---

### R-3 — Phase 3 has the same class of bug as Phase 1

**What it is**  
Phase 3 (afternoon SPX proximity exit) also has a “should I watch this?” check that can be true for **any open trade after 2:51 PM** — not only when a real exit condition fired.

**Risk**  
Same mistake as July 6, but only relevant **late in the day** for open trades.

**Your decision**  
“Fine for now” — because **F-3 tonight fixes the same pattern for all phases**, including Phase 3. No separate patch needed.

---

### R-4 — We need to update an automated test (why it matters)

**What it is**  
There is an automated test that checks: *If the bot restarts during a manual close, does it finish the close correctly?*  
That test is **good** and should stay.

**What’s missing**  
We also need a test that says: *If the bot restarts with a **false breach** flag, it must **NOT** send a close order.*

**Why you should care**  
Without that second test, we could “fix” the code but the test suite would still think the old broken behavior is correct. Tests are the bot’s memory of what “right” looks like.

**Plain analogy**  
You fixed a door lock, but if the security checklist still says “leave key under mat,” someone will put it back.

**Decision needed:** None — this is implementation work tonight.

---

### R-5 — Heavy broker work should not block the whole bot

**What it is**  
The stop monitor checks all trades several times per second. **Placing or cancelling orders** through Tasty takes longer and should happen on a **background worker**, not on the fast check loop.

**Why it matters**  
If order placement runs on the fast loop, one slow Tasty call can delay watching **all** your trades.

**July 6 connection**  
The false breach triggered exit logic on the fast path. The fix keeps the fast path for **decisions only** (“should we exit?”) and the slow path for **actions** (“send the close order”).

**Plain analogy**  
The manager decides “we need to order supplies” in the meeting; a separate person actually calls the vendor. The meeting doesn’t pause for hold music.

**Decision needed:** None — build it this way tonight.

---

### R-6 — Don’t ask Tasty “is my stop working?” hundreds of times per minute

**What it is**  
Before breach watching starts, we want to confirm the exchange stop is live at Tasty. That requires an API call.

**Risk**  
If we do that on **every** fast scan (4× per second × number of trades), we hammer Tasty’s API — same family of problem as IP blocking.

**The fix**  
Check stop status on a **slow timer** (about every 10 seconds). Cache the result. Fast scans only read the cached answer.

**Plain analogy**  
Check your mailbox once when you get home, not every time you walk past the front door.

**Decision needed:** None — build it this way tonight.

**Your addition (30-second long-leg rule):**  
When the slow path sees the exchange stop **filled**, read the **broker fill time** (not “when we noticed”). Wait a full **30 seconds from that fill time** before starting the long-leg close.

Example: stop filled at 10:00:00, we check at 10:00:09 → wait **21 more seconds** (not a fresh 30 from 10:00:09).

**Note for tonight’s build:** Today the code records `short_closed_at` when we **detect** the fill (`time.time()` at detection). That should change to use Tasty’s fill timestamp when available, so the 30s clock starts at the real fill.

---

### R-7 — If the bot crashes mid-breach-exit, resume the breach exit — not manual kill

**What it is**  
If the bot dies **after** cancelling a stop but **before** sending the spread close (real breach path), recovery should **continue the breach exit** — not switch to manual kill.

**Your decision**  
Sounds right — agreed.

---

### R-8 — None of the P0 fixes are in the code yet (as of evening Jul 6)

**What it is**  
The repair plan is written. The code still behaves like July 6 until we implement tonight.

**Your decision**  
Fix V3 tonight; go live tomorrow after checklist.

---

## Part 5 — Your decisions (Q1–Q8) — summary

| Question | What you decided |
|----------|------------------|
| **Q1 — Tomorrow’s plan** | Fix tonight. V3 live tomorrow after morning script + quick validation. |
| **Q2 — How to build F-3** | Use the cleaner three-answer (PhaseAction) approach. |
| **Q3 — Dashboard F-10** | Yes — include before live. |
| **Q4 — When to implement** | All tonight. |
| **Q5 — Skip checklist?** | No — not skipping. |
| **Q6 — Orphan process check** | Script added: `scripts/check_stop_monitor.py`. Run before open. |
| **Q7 — `.env` engine** | `v3` tomorrow after checklist passes. Rollback: set `v2`, restart. |
| **Q8 — Anything missing?** | (You can add notes here.) |

---

## Part 6 — Tomorrow morning: your simple checklist

Do these in order before the market opens.

### 1. Check for stray bot processes (2 minutes)

```powershell
cd MEIC-with-Dash-main-V2
uv run python scripts/check_stop_monitor.py
```

| Result | Meaning | Action |
|--------|---------|--------|
| **0 processes** | Clean — launcher not running yet | Good. Start `run.py` when ready. |
| **1 process** | One stop monitor | OK **only if** you already started the launcher. |
| **2+ processes** | Orphan risk | Run `uv run python scripts/check_stop_monitor.py --kill` |

### 2. Confirm fixes were applied last night

Ask whoever implemented (or check yourself):

- [ ] All P0 fixes (F-3 through F-9) done
- [ ] F-10 dashboard fix done
- [ ] Automated tests pass (`pytest tests/ -q`)

### 3. Quick paper or dry run (recommended even if brief)

- [ ] Open a paper trade OR watch first live tranche closely
- [ ] After entry: **no** immediate close in the first 30 seconds
- [ ] Logs should **not** show `Resuming manual kill` with reason `breach_phase1_initial_stop` right after open
- [ ] If you manually close a test trade: closes **once**, not twice

### 4. `.env`

- [ ] `STOP_MONITOR_ENGINE=v3` only if the above are green
- [ ] Know rollback: change to `v2` → save → restart `run.py`

### 5. During the session — red flags (stop and switch to v2)

If you see any of these on a **new** entry:

- Trade closes within seconds with no stop hit and no button press
- Log line: `Resuming manual kill ... breach_phase1_initial_stop` right after open
- Two close orders for the same leg
- Dashboard PnL wildly wrong on a normal trade

**Immediate action:** Stop launcher → set `STOP_MONITOR_ENGINE=v2` in `.env` → restart → review logs.

---

## Part 7 — Glossary (terms you might see in logs)

| Term | Plain meaning |
|------|----------------|
| **Stop monitor** | Background program that watches open trades, manages stops, and handles exits |
| **V2 / V3** | Two versions of that program. V3 is newer but had the July 6 bug. V2 is the safe fallback. |
| **Launcher** | `run.py` — starts streamer, stop monitor, and scheduled tranches together |
| **Phase 1** | Early day — initial stop and breach rules |
| **Phase 2** | Mid day — may upgrade stop; should **not** close the trade |
| **Phase 3** | Late day — SPX proximity / time-based exit rules |
| **Breach** | Software rule says “exit now” (e.g. spread mid price crossed threshold) |
| **Exchange stop** | Stop order resting at Tasty (broker-side) |
| **Manual kill / manual close** | You clicked close on the dashboard |
| **close_only_mode** | Internal flag: “this trade is in exit process” |
| **exit_handler** | Internal label for *why* we’re exiting (manual, breach, Phase 3, etc.) |
| **MQTT** | Live price feed from the streamer into the bot |
| **Heartbeat** | `trades/heartbeat.json` — proves stop monitor is alive; `ts` should update while running |
| **Orphan process** | Old stop monitor still running after you thought everything was stopped |
| **Flat account** | No open position for that spread |
| **BTO/STO** | Buy-to-open / sell-to-open — opens a **new** position (what happened on Round 2) |
| **BTC/STC** | Buy-to-close / sell-to-close — closes an **existing** position (what Round 1 did correctly) |
| **Paper mode** | Fake money / test account |

---

## Part 8 — The one rule to remember

```
No real breach
No button pressed
No confirmed late-day Phase 3 exit
─────────────────────────────
= NO close order
= NO second close
= NO accidental new spread at Tasty
```

If the fixed V3 ever violates that on a new entry, **stop and roll back to V2**.

---

## Part 9 — Where to add your notes

Use this space for anything still unclear or priorities for tomorrow:

```
(your notes here)


```

---

## Related files

| File | Purpose |
|------|---------|
| [STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md](STOP_MONITOR_V3_INCIDENT_2026-07-06_UPDATED_CURSOR_READY.md) | Full technical repair plan for Cursor |
| [STOP_MONITOR_V3_INCIDENT_2026-07-06.md](STOP_MONITOR_V3_INCIDENT_2026-07-06.md) | Original incident write-up |
| [V2_README.md](../V2_README.md) | How to run `check_stop_monitor.py` |
| `scripts/check_stop_monitor.py` | List/kill stray stop-monitor processes |
