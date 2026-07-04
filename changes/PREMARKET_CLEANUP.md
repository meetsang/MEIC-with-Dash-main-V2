# Session Cleanup — Morning & End-of-Day

**Date**: Jun 23, 2026  
**Status**: Implemented  
**Code**: `common/session_cleanup.py`, wired from `run.py`  
**Related**: [OPERATIONAL_HARDENING.md](OPERATIONAL_HARDENING.md), [MANUAL_STRATEGY.md](MANUAL_STRATEGY.md)

---

## Problem

After **3 PM tests**, leftover state breaks the **next trading day**:

- Stale `optsymbols.json` (streamer subscribes to junk at 8:30)
- Active JSON in `meic0dte/trades/active/` and `manual_spread/trades/active/`
- `pause_tranches.json` still pausing all MEIC slots
- `killswitch.json` and stale `commands/*.json`

Cleanup used to happen mostly at **3:00 PM** inside the streamer and stop_monitor. That is too early for after-hours testing and mixed poorly with **broker admin close** logic.

---

## Schedule (Central time)

| When | Mode | Trigger |
|------|------|---------|
| **~8:20 AM** | `morning` | `run.py` outer loop, before `main()` starts streamer |
| **3:30 PM** | `eod` | `run.py` `main()` finally, after 3:00 PM session loop exits |

**3:00 PM** still stops the trading session (streamer + stop_monitor). **3:30 PM** runs full cleanup.

---

## What each cleanup pass does

Always (both modes):

1. Reset `streaming/optsymbols.json` → `{"SYMBOLS": []}`
2. Clear `meic0dte/trades/pause_tranches.json` (`paused_slots: []`)
3. Remove `meic0dte/trades/killswitch.json` if present
4. Delete stale `*.json` in `meic0dte/trades/commands/` and `manual_spread/trades/commands/`

Then **archive eligible active trades** from both:

- `meic0dte/trades/active/`
- `manual_spread/trades/active/`

into `…/history/YYYY-MM-DD/` (Central calendar date).

---

## Expiry rules (your approved policy)

Expiry is read from **option leg symbols** (e.g. `.SPXW260624P7230` → 2026-06-24), with filename fallback.

| Mode | Archive when | Keep when |
|------|--------------|-----------|
| **morning** (8:20) | expiry **< today** | today or **future** expiry |
| **eod** (3:30) | expiry **≤ today** | **future** expiry only |

Examples on **2026-06-24**:

- **Morning**: yesterday’s 0DTE → archived; tomorrow manual PCS → kept  
- **EOD**: today’s 0DTE → archived; tomorrow manual PCS → kept  

---

## 3 PM admin close — removed for SPX

Previously `stop_monitor` at 3:00 PM attempted `_close_long_leg()` + `_finalize_close(market_close_3pm)` and archived all active files.

**SPX 0DTE is cash-settled at expiry.** Positions do not require a bot-driven flatten at 3:00 PM for production. That path caused errors after hours (`Option not found in chain`) and duplicated cleanup.

**Now:**

- stop_monitor keeps running until launcher stops it at **3:00 PM**
- No broker admin close at 3:00 PM for SPX
- **3:30 PM** `eod` cleanup archives JSON by expiry rules above

Non-SPX underlyings may need different logic later.

---

## Operator notes

- **Pause All MEIC** is cleared every cleanup — re-pause on volatile days after 8:20.
- **Manual Spread** with **tomorrow expiry** survives same-day EOD cleanup (future expiry kept).
- After-hours tests: **EOD cleanup at 3:30** clears today’s 0DTE artifacts; **morning cleanup** catches anything expired before today.
- No separate manual cleanup script — use the scheduled passes only.

---

## Logs

Look for:

```
Session cleanup (morning) — Central date 2026-06-24
Cleanup done (morning): MEIC archived=2 kept=0 | Manual archived=0 kept=1 | commands=0
```

and at 3:30:

```
Waiting until 15:30 for end-of-day cleanup ...
Session cleanup (eod) — Central date 2026-06-24
```

---

## Tests

```bash
pytest tests/test_session_cleanup.py -q
```
