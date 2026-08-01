# Skyline Paper-Day Log — 2026-07-31

**Branch:** `feat/multi-strategy-framework`  
**Mode:** Journal paper (live market data, local fill recording)  
**MEIC:** Live (`PAPER_MODE=false`), all tranches paused for test day  

---

## Config snapshot

```yaml
SKYLINE_SPX:
  enabled: true
  confirmed: true
  paper: true
  entry_time_et: '09:45'  # yaml default; actual entry was manual test ~10:59 ET
```

---

## Entry

| Field | Value |
|-------|-------|
| Date | 2026-07-31 |
| Entry time (ET) | 10:59:42 |
| ATM | 7450 |
| Gap-down skip | Not triggered |
| All 4 butterflies | Filled (journal) |

### Per-butterfly (strikes / debit)

| Fly | Strikes | Net debit (pts) |
|-----|---------|-----------------|
| BF1 | 7450/7475/7500 call | 5.92 |
| BF2 | 7490/7510/7525 call | 1.05 |
| BF3 | 7455/7430/7405 call | 5.05 |
| BF4 | 7415/7390/7370 put | 2.08 |
| **Total** | | **14.10** |

---

## Intraday P/L (reconstructed)

| Metric | Value | Source |
|--------|-------|--------|
| Max intrinsic proxy | +$1,090 | SPX polls @ 12:02 CT, SPX=7475 (not true MTM) |
| EOD realized | **−$365** | Settlement SPX=7489.55 |
| Option mids recorded | **No** | `options_quotes.csv` missing — legs not in streamer until fix |

Per-structure EOD:

| Fly | Realized |
|-----|----------|
| BF1 | +$453 |
| BF2 | −$105 |
| BF3 | −$505 |
| BF4 | −$208 |

---

## Incidents

1. **Broker deadlock (09:46–11:51):** `get_shared_broker_for_strategy()` held lock while calling `get_shared_broker()` — blocked MEIC 11:00 entry workers. **Fixed** before end of session.
2. **MEIC 11:00 miss:** Cosmetic `entering` state on session CSV; no live order placed.
3. **No streamer leg registration:** Skyline legs not added to `optsymbols.json` — no `options_quotes.csv` for Jul 31. **Fixed** post-session.

---

## Manual close exercised?

No — held to expiration (paper journal).

---

## EOD settlement

| Field | Value |
|-------|-------|
| SPX close | 7489.55 |
| Source | mqtt_settlement |
| File | `trades/history/SKYLINE_SPX/skyline_2026-07-31.json` |

---

## Post-session fixes shipped

- Skyline legs → `optsymbols.json` after entry (+ re-register on restart)
- `options_quotes.csv` interval: 3 min → **1 min** (MQTT cache only, no REST)
- Dashboard: strikes column, row contrast, auto-refresh, overrides UI, dollar P/L display
- `scripts/skyline_analyze_pnl.py` for future max-MTM analysis

---

## Operator sign-off

- [x] Four butterflies entered and visible on dashboard
- [x] EOD settlement moved to history
- [x] Paper P/L excluded from live MEIC totals
- [ ] Re-run with streamer leg registration to capture true intraday MTM
