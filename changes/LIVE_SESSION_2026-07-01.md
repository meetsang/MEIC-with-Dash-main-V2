# Live Session Notes — Jul 1, 2026

**Status:** Operator log + incident notes. Overnight launcher did **not** resume at 08:20 CT; operator manually restarted at **11:02**.  
**Related:** [LIVE_SESSION_2026-06-30.md](LIVE_SESSION_2026-06-30.md), [LIVE_SESSION_2026-06-29.md](LIVE_SESSION_2026-06-29.md)

---

## Incident — Overnight launcher never woke; missed automatic morning session

### What the operator saw

- Left `python run.py` running after **Jun 30** session (launcher completed EOD cleanup and logged “sleeping until next trading day”).
- On **Jul 1**, expected bot to resume at **08:20 CT** (morning cleanup + streamer at 08:30 + first tranche at 11:00).
- Bot **did not come back up** on its own. No automatic trades before operator intervention.
- At **~11:01 CT**, operator interrupted the still-sleeping launcher (`Ctrl+C`) and restarted at **11:02**.

### Timeline (from `logs/launcher_2026-06-30_075721.log`)

| Time (CT) | Event |
|-----------|--------|
| **Jun 30 15:00** | Session shutdown (3 PM stop) |
| **Jun 30 15:30** | EOD cleanup — 12 MEIC + 2 manual JSON archived |
| **Jun 30 15:30** | `Sleeping until next trading day 2026-07-01 08:20 (16.8h)` |
| **Jul 1 08:20** | *(expected wake — **no log activity**)* |
| **Jul 1 08:30** | *(expected streamer start — **did not happen**)* |
| **Jul 1 10:59** | *(expected 11-00 entry workers — **did not happen**)* |
| **Jul 1 11:01:51** | `Launcher interrupted by user.` — process was still in overnight sleep |
| **Jul 1 11:02:01** | Fresh start (`logs/launcher_2026-07-01_110201.log`) — morning cleanup, streamer, stop monitor |

**Gap:** ~2h 42m past scheduled resume before operator noticed and restarted.

### Root cause (suspected)

| Layer | Issue |
|-------|--------|
| **Overnight sleep** | `run.py` uses a single `time.sleep(secs)` for the inter-day gap (~line 541). |
| **Intraday waits** | `wait_until(hour, minute)` recalculates against **wall-clock Central time** every 1–30s — survives clock drift and is the pattern used for stream start (08:30) and EOD cleanup (15:30). |
| **Windows suspend** | `time.sleep()` **pauses** when the PC sleeps/hibernates. Timer does not advance during suspend, so the bot can miss the 08:20 target even though wall-clock time has passed. |

Evidence: launcher logged sleep at 15:30 Jun 30 and had **no further log lines** until operator interrupt at 11:01 Jul 1 — consistent with sleep timer still counting down after machine suspend.

### Manual restart outcome (11:02 CT)

After operator restart, bot ran normally for the **11-00** tranche (late by ~3 min):

| Lot | Side | Order | Credit | Stop | Status |
|-----|------|-------|--------|------|--------|
| 11-00 | CALL | 480317305 | 0.90 | 2.00 (7540/7565 CCS) | open |
| 11-00 | PUT | 480317429 | 1.10 (chase after cancel) | 3.20 (7490/7465 PCS) | open |

- PUT first attempt (480317341) cancelled after 5s no-fill; chase fill on second attempt.
- Minor 429 rate-limit on parallel PUT/CALL SPX quote fetch at entry — retried successfully.

**Operator takeaway:** Restart fixed the session, but the **scheduled overnight path is unreliable** if the host sleeps.

---

## Open / follow-up

| Item | Status |
|------|--------|
| Replace overnight `time.sleep(secs)` with `wait_until_central()` loop (same as intraday) | **Fixed** — `run.py` + `tests/test_wait_until_central.py` |
| Windows power settings: prevent sleep during trading week / use “Never sleep on AC” | Operator config |
| Heartbeat or status file while sleeping (“resumes Tue Jul 1 08:20”) | Nice-to-have |
| Alert if 08:30 passes with no streamer PID | Nice-to-have |
| Document: overnight `run.py` requires machine awake at 08:20 CT | This note |

### Fix shipped

**`run.py`:**

- New `wait_until_central(target)` — polls `_central_now()` every 1–30s instead of one long `time.sleep(secs)`.
- Overnight (post-EOD) and weekend waits now use `wait_until_central()`.
- `wait_until(hour, minute)` delegates to the same helper.

**Operator:** Restart **launcher** (`python run.py`) for the fix to take effect on the next overnight cycle.

### Operator workaround (if not yet restarted)

1. **Disable PC sleep** on trading nights (or leave machine awake).
2. **Or** stop launcher at EOD and start fresh each morning before 08:30 CT (`python run.py`).
3. If dashboard shows “Done for today. Resuming …” but nothing happens at 08:20, check whether the launcher process is still running and whether the PC was suspended.

---

## Parked — 01-15 CCS failed (overlap with 11-00)

**Status:** Documented, not fixed. See [STALE_PENDING_TRADE_JSON.md](STALE_PENDING_TRADE_JSON.md) scan-pick gap.

### What happened

At **13:14 CT**, **01-15 CALL** terminated after **3 scan-pick retries** (`SCAN_PICK_RETRIES = 3`). Retries are for **transient** empty scans / rate limits — not persistent overlap.

| Attempt | Scan pick | Result |
|---------|-----------|--------|
| 1/3 | 7515/7540 CCS @ $1.00 | Overlap: long **7540** = short **7540** in open **11-00** CCS; shift 7510/7535 out of credit band |
| 2/3 | Same | Same |
| 3/3 | Same | `Entry terminated` — never placed an order |

**Chase phases** (`chase_same_trade` ×3, `build_new_strikes` ×7) never ran — scan pick failed first.

### GEX at same time?

Possible contributor to **429** bursts when tranche fires (PUT + CALL workers in parallel). **Not root cause** — scan had quotes (55/67 symbols) and failed on overlap logic, not empty scan.

### Recommended fix (deferred)

Skip to next **non-overlap** in-band candidate instead of retrying the same overlapping pick.

---

## Incident — PCS breach long-close sent at $7504.30 (12-30 + 01-15)

### What the operator saw

Both **12-30** and **01-15** PCS (**7485/7460**) breached; exchange stop closed **7485** short on platform. Bot then spammed **SELL_TO_CLOSE** on **7460** long at **$7504.30 credit** while market was **~$0.30**. Operator paused bot and filled long manually.

### Root cause — MQTT SPX fallback bug

`common/mqtt_prices.py` `get()` / `get_market_mid()` included **`'SPX'`** in the alias key list for **every** symbol lookup. When `.SPXW260701P7460` was missing from the MQTT cache, lookup fell through to the **SPX index** mid (~**7504**).

Stream log at breach time shows SPX ask **7504.55** alongside P7460 bid/ask **0.35/0.40** — matches the bogus order price.

**Not a GEX/resource issue** — wrong price source when option MQTT mid was absent.

### Timeline

| Time (CT) | Lot | Event |
|-----------|-----|-------|
| 12:29 | 12-30_P | Filled 7485/7460 PCS; stop 480374803 @ 2.65 |
| 13:14 | 01-15_P | Filled 7485/7460 PCS; stop 480398075 @ 3.40 |
| 13:15–13:16 | 12-30_P | Stop filled; long chase with $7504 limits (**12 attempts** in JSON) |
| 13:16 | 01-15_P | Stop filled; status `closing` — same chase loop |
| Operator | Both | Paused bot; manual long close **7460** @ **$0.30** (01-15) and **$0.65** (12-30) |

### Fix shipped

| File | Change |
|------|--------|
| `common/mqtt_prices.py` | Removed SPX from option lookup aliases; `get_spx()` unchanged |
| `blocks/stop/monitor.py` | Reject long mid **> $20** as index noise; floor at $0.05 |
| `tests/test_mqtt_prices_spx_fallback.py` | Regression test |
| `scripts/finalize_jul01_pcs_breach.py` | Operator reconcile — archive both PCS as `closed` |

### Operator reconcile (run once)

```powershell
cd MEIC-with-Dash-main-V2
.venv\Scripts\python.exe scripts\finalize_jul01_pcs_breach.py
```

Marks **12-30_P** long @ **0.65**, **01-15_P** long @ **0.30**, moves JSON out of `trades/active/`, updates session CSV to **closed**. Safe to **unpause** launcher after running.

**Operator:** Restart **launcher** after deploy so stop monitor loads MQTT fix.
