# Live Session Notes — Jul 27, 2026

**Status:** **10-00 MEIC did not fire** (investigated ~10:09 CT). **Resolved ~6:09 PM** — stashed Jul 24 changes restored and committed (`10-00` … `01-15` schedule + dashboard PnL fix). Restart launcher before next session so bootstrap picks up new slots.

**Root cause:** Morning bootstrap reset to the default 6-lot schedule (`11-00` through `02-00`) because Jul 24 code changes were **stashed but never committed** before the REST governor work (`stash@{0}: wip-before-account-wide-rest-governor`).

**Related:** [LIVE_SESSION_2026-07-23.md](LIVE_SESSION_2026-07-23.md), [LIVE_SESSION_2026-07-24 session CSV](../trades/session/MEIC_IC_2026-07-24.csv) (had `10-00`/`10-30`), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md)

---

## Operator question — why didn't today's 10 AM tranche execute?

### Short answer

The **`10-00` lot is not in today's session plan**. At **08:00 CT** the launcher bootstrapped a fresh `MEIC_IC_2026-07-27.csv` from the code default (`MEIC_TRANCHE_SLOTS` in `strategies/meic/strategy.py`) — **12 rows, 6 lots starting at `11-00`**. There are **no `10-00_P` / `10-00_C` rows** for the entry monitor to fire.

During the **09:59–10:05 CT** window the bot was up and healthy (entry monitor ~5,800+ ticks, streamer live), but it had **nothing scheduled** for that window:

- No `pre_tranche` REST probe for `10-00` in the launcher log
- No `Spawned entry worker for 10-00_P/C` lines
- `runtime/trading_gate.json` → `probes_by_tranche: {}` (only startup probe recorded)

This is **not** a gate block, crash, or entry-monitor stall — the tranche simply **was not on today's plan**.

On **Jul 24**, `10-00` fired normally because those rows existed in that day's session CSV (window `09:59–10:05`). That customization is **not persisted** across days unless encoded in `MEIC_TRANCHE_SLOTS` or re-added to the session CSV before the window.

---

## Day summary — MEIC tranches

| Tranche | Window (CT) | Outcome | Notes |
|---------|-------------|---------|-------|
| **10-00** | 09:59–10:05 | **Not scheduled** | Absent from today's session CSV |
| **10-30** | 10:29–10:35 | **Not scheduled** | Also absent — will miss unless added before 10:29 |
| **11-00** | 10:59–11:05 | pending | First lot on today's default plan |
| 12-00 | 11:59–12:05 | pending | |
| 12-30 | 12:29–12:35 | pending | |
| 01-15 | 13:14–13:20 | pending | |
| 01-45 | 13:44–13:50 | pending | |
| 02-00 | 13:59–14:05 | pending | |

---

## Timeline (CT)

| Time | Event | Source |
|------|-------|--------|
| **08:00:07** | Launcher started (`run.py`, live TastyTrade) | `logs/launcher_2026-07-27_080007.log` |
| **08:00:08** | **Bootstrapped session plan** `MEIC_IC_2026-07-27.csv` **(12 rows)** — default lots only | launcher log |
| **08:00:08** | Archived Jul 24 trades including `10-00_*` / `10-30_*` (expiry settlement) | launcher log |
| **08:00:11** | Startup REST probe **OK** (132 ms) | launcher log + `runtime/trading_gate.json` |
| **08:30:00** | Streamer, market_data, stop_monitor started | launcher log |
| **08:30:11** | Entry monitor started — `pending_meic=12` | launcher log |
| **09:58–10:05** | **10-00 window — no probe, no spawn, no orders** | launcher log (heartbeats only) |
| **~10:09** | Investigation: all 12 session rows still `pending`; infra healthy | session CSV + health files |

---

## Evidence — 10-00 did not fire

| Check | Result |
|-------|--------|
| `trades/session/MEIC_IC_2026-07-27.csv` | **No `10-00_P` / `10-00_C` rows** — first lot is `11-00` |
| `trades/session/MEIC_IC_2026-07-24.csv` (prior day) | Had `10-00` + `10-30` rows with window `09:59–10:05` / `10:29–10:35` |
| `strategies/meic/strategy.py` `MEIC_TRANCHE_SLOTS` | Default 6 lots: `11-00` … `02-00` only — **no `10-00`** |
| Launcher log 09:59–10:05 | Heartbeats only — **no** `pre_tranche tranche=10-00`, **no** `Spawned entry worker` |
| `trades/active/MEIC_IC/10-00_*` | **None** for today |
| `runtime/trading_gate.json` | `rest_status=healthy`, `new_risk_latched=false`, `probes_by_tranche={}` |
| Entry monitor | Healthy — `tick_count` ~5867, `pending_meic=12`, `last_tick_duration_sec` ~0.001 |
| Streamer | Live — `status=live`, SPX updating at ~10:09 CT |

### Jul 24 contrast (10-00 worked)

From `logs/launcher_2026-07-24_085118.log`:

```
09:58:31 REST probe starting source=pre_tranche tranche=10-00 strategy=MEIC_IC
09:58:31 REST probe finished source=pre_tranche tranche=10-00 ok=True ...
09:59:00 Spawned entry worker for 10-00_P (MEIC_IC)
09:59:03 Spawned entry worker for 10-00_C (MEIC_IC)
```

---

## Root cause

### Session bootstrap resets custom lots daily

`blocks/session/bootstrap.py` creates today's CSV **only if the file is missing**, using `MEIC_TRANCHE_SLOTS`:

```python
slot_list = slots or list(MEIC_TRANCHE_SLOTS)  # 6 lots → 12 rows
```

Each new trading day gets a **fresh CSV** from code defaults. Dashboard session-plan edits (like Jul 24's `10-00` / `10-30` rows) **do not roll forward**.

After bootstrap, `run.py` refreshes the probe coordinator from the session CSV (`meic_tranches_from_session_plan`). With no `10-00` rows:

1. Probe coordinator never schedules a `10-00` pre-tranche probe
2. Entry monitor never evaluates `10-00_P` / `10-00_C` during `09:59–10:05`
3. Window passes silently — no `TRANCHE_MISSED` because there was no row to miss

### What was ruled out

| Hypothesis | Ruled out by |
|------------|--------------|
| REST gate / latch | `rest_status=healthy`, `new_risk_latched=false` |
| Entry monitor stall | Continuous heartbeats, sub-ms tick duration |
| Launcher down during window | Log continuous from 08:00 through 10:09+ |
| Tranche paused | N/A — rows don't exist |
| PC asleep | Streamer live with fresh SPX quotes |

---

## Immediate operator options

### If you still want 10-00 exposure today (window closed)

1. **Manual catch-up** — add `10-00_P` / `10-00_C` rows to today's session CSV with widened `entry_window_end`, then use dashboard **Re-check REST** or wait for next tranche probe; **or** run off-hours fire:
   ```bash
   python run.py --tranche-now --lot 10-00 --force
   ```
   (Requires `10-00` in session CSV and compatible slot config.)

2. **Before 10:29** — if you also want `10-30`, add those rows now or codify both lots in code (below).

### Permanent fix — codify early tranches in code

Add `10-00` and `10-30` to **all** of these (they must stay in sync per README):

| File | What to add |
|------|-------------|
| `strategies/meic/strategy.py` | `TrancheSlot('10-00', time(9, 59), time(10, 5), 'MEIC_IC')` and `10-30` |
| `dashboard/server.py` `TRANCHE_LOTS` | `'10-00'`, `'10-30'` at front of list |
| `dashboard/templates/index.html` `LOTS` | Same |
| `meic0dte/app/utilities.py` `lot_times` | Matching windows (legacy path) |

After code change, **delete or rename** today's session CSV and restart launcher **before** the next window so bootstrap picks up the new slots — **or** manually append the missing rows to the existing CSV for today.

---

## Infra notes (non-blocking)

- Launcher, streamer, stop monitor all healthy at investigation time
- Ladder disabled (`ladder_enabled=false`) — same as recent sessions
- `10-30` will miss today too unless rows are added before **10:29 CT**

---

## Files referenced

| File | Role |
|------|------|
| `trades/session/MEIC_IC_2026-07-27.csv` | Today's plan — no `10-00` rows |
| `trades/session/MEIC_IC_2026-07-24.csv` | Prior day — had `10-00` / `10-30` |
| `logs/launcher_2026-07-27_080007.log` | Bootstrap + silent 10-00 window |
| `runtime/trading_gate.json` | Healthy; no per-tranche probes yet |
| `strategies/meic/strategy.py` | Default slot source for bootstrap |
