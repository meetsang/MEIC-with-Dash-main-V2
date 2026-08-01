# Live Session Notes — Jul 7, 2026

**Status:** Session complete — see [CHANGES_SINCE_2026-07-06.md](CHANGES_SINCE_2026-07-06.md) (EOD entry).  
**Related:** [LIVE_SESSION_2026-07-06.md](LIVE_SESSION_2026-07-06.md), [STOP_MONITOR_V3_INCIDENT_2026-07-06.md](STOP_MONITOR_V3_INCIDENT_2026-07-06.md)

---

## Session plan — Jul 7, 2026

| Time (CT) | Item |
|-----------|------|
| **AM** | V3 live validation — manual spread entry + stop placement |
| **Ongoing** | Confirm clean manual close path (no Jul-6-style duplicate orders) |

---

## Observations

| Time (CT) | Event | Notes |
|-----------|-------|-------|
| 08:43 | ms-184 opened | Put credit spread 7445/7400, qty 3, limit $0.55 credit |
| 08:46 | ms-184 stop armed | Exchange stop `481369510` @ $2.20; breach watch armed |
| 09:14–09:15 | ms-184 closed via exchange stop | Short leg stopped; long chased and filled. `exit_handler` = `exchange_stop`. **Did not exercise manual-close / F-9 preflight path.** |
| 09:16 | ms-185 opened | Put credit spread **7425/7400**, qty **6**, limit $0.65 credit, filled @ ~$0.70 net |
| 09:16 | ms-185 stop armed | Exchange stop `481416212` @ $2.45 (phase 1, 2× short); breach armed |
| 09:42 | ms-185 breach watch | Spread ~$1.12–1.20 vs threshold $1.60 — watching, no software exit |
| 11:04–11:07 | Streamer stale (ms-185) | `Streamer prices stale (>30s)` — software breach checks frozen (expected F-3 behavior). Exchange stop still live. |
| **11:30** | **ms-185 operator flat @ $0.05 db** | Manual Tasty close after bot preflight failure. Archived: `brokerage_spread_exit_debit=0.05`, `close_mechanism=operator_manual`. Net vs entry ($0.70 cr): **+$0.65/sp** |

---

## Incident — ms-185 manual close (11:17 CT)

### Operator report

Dashboard **Close** on **ms-185**: stop was cancelled at Tasty, but **no spread-close order was submitted**.

### What the system did (timeline)

| Step | Time (CT) | Result |
|------|-----------|--------|
| Dashboard claims manual close | 11:17:18 | `Claimed manual close … mechanism=manual_close` |
| Stop cancelled | 11:17:18 | `DELETE …/orders/481416212` → 200 OK; WS `Cancelled` |
| Spread close preflight | 11:17:19+ | **Blocked** — `manual_kill_skip_not_closable … position_state=mismatch` |
| Recovery loop | 11:17:19 – 11:21:59+ | V3 recovery re-routed `manual_close → ManualKillHandler` repeatedly (~**450+** preflight failures); **no** `place_spread_close` log line ever |

### Trade JSON state (after failure)

File: `trades/active/MANUAL_SPREAD/ms-185_P_20260707T091639.json`

| Field | Value |
|-------|-------|
| `status` | `open` |
| `quantity` / `filled_quantity` | 6 / 6 |
| `active_stop` | `null` (cancelled) |
| `close_only_mode` | `true` |
| `close_mechanism` | `manual_close` |
| `exit_handler` | `manual_close` |
| `exit_last_step` | `preflight` |
| `exit_error` | `preflight_mismatch` |
| `spread_close_order_id` | `null` |

Stop history records cancel reason `spread_close_cancel:manual_close` at 11:17:18.

### Root cause (code analysis — no live broker call)

**F-9 preflight (`inspect_spread_position`) mis-reads Tasty position quantities.**

Tasty reports option positions as **positive `quantity` + `quantity-direction`** (`Short` / `Long`), e.g. from WS at ms-185 entry:

```
quantity: 6, quantity-direction: Short   (7425P)
quantity: 6, quantity-direction: Long    (7400P)
```

`brokers/tastytrade_broker.py` → `inspect_spread_position()` uses raw `pos.quantity` and expects:

- short leg: **negative** qty (`short_qty < 0`)
- long leg: **positive** qty (`long_qty > 0`)

With Tasty’s unsigned qty, the short leg reads as `+6`, so `short_closable` is **false** → returns **`mismatch`** even when the account holds a valid 6-lot vertical.

`spread_close_preflight_blocked()` treats `mismatch` as a hard block → `ManualKillHandler` never calls `place_spread_close_order()`.

### What did **not** cause this

- Not missing quotes (exit never reached `resolve_quotes`)
- Not broker cooldown / 401 (positions API returned 200 OK each attempt)
- Not flat account (would be `flat`, not `mismatch`)
- Not Jul-6 false breach (manual operator close; breach watch was OK at click time)

### Operator impact

- **Naked short put exposure** after stop cancel — position still open at Tasty with **no exchange stop** and **no bot close order**
- Recovery loop hammers `GET /positions` every ~100–300 ms while `close_only_mode` + `exit_error` persist

### Immediate operator action (manual, outside bot)

1. Confirm at Tasty: 6-lot 7425/7400 put vertical still open, stop gone
2. **Close manually at Tasty** (or re-place protection) — do not rely on bot until preflight is fixed
3. After flat: archive/clean `ms-185` active JSON if needed

### Fix direction (deferred — not implemented this session)

- Normalize Tasty position qty using `quantity-direction` (or equivalent REST field) before F-9 closable check
- Add preflight debug log: short_qty, long_qty, expected_qty, direction fields
- Recovery backoff when `exit_error=preflight_mismatch` repeats (avoid position API storm)
- Unit test with Tasty-style unsigned position objects

### Fix applied (same session, ~11:25 CT)

- `brokers/tastytrade_broker.py` — `_signed_position_qty()` reads Tasty `quantity_direction` (`Short` → negative, `Long` → positive) before F-9 closable check
- Tests: `tests/test_inspect_spread_position.py` (ms-185 7425/7400 scenario)
- **Operator:** restart stop monitor, then retry dashboard close on ms-185 (or close manually if already flat)

---

## Earlier today — what validated

| Check | ms-184 | ms-185 |
|-------|--------|--------|
| Entry + fill | ✓ | ✓ |
| Exchange stop placed | ✓ | ✓ |
| Breach arm / watch | ✓ | ✓ |
| Manual dashboard close | — | **✗ preflight_mismatch** |
| Exchange stop exit | ✓ | — |

---

## Tabled — software breach vs 2× net credit (afternoon review)

**Status:** Deferred for operator review before any code change.

Afternoon put breach wave (~13:49 CT) closed **12-30_P** and **01-45_P** via `software_breach` even though brokerage stops were missing or shared across tranches. Resilience worked (positions flat), but **operator slippage was large** (−$95 to −$105 per lot vs 2× credit policy).

| Topic | Finding |
|-------|---------|
| **Detection** | Spread mid vs `two_x_net_credit + $0.20` — fired correctly (e.g. 12-30_P: spread $1.30 ≥ threshold $1.10) |
| **Slippage math** | **Correct** — compares actual spread exit debit to `two_x_net_credit` (policy target) |
| **Execution gap** | Breach places short-leg limit at **live short mid** (`replace_with_limit_close`), not at a price that caps spread debit to 2× credit |
| **Reprice chase** | `breach_limit_reprice` can raise limits into a fast market (e.g. 01-45_P $1.75 → $2.05) |
| **Shared-stop interaction** | Breach **cancelled** the reconciled exchange stop on 7485 before placing limits — turned backup into primary exit |
| **Doc drift** | `TESTING.md` still says `two_x_short + 0.20`; code uses `two_x_net_credit + 0.20` (`test_spread_breach_threshold.py`) |

**Operator takeaway:** Large negative slippage on breach exits is reporting a real gap between the **2× credit policy** and **market-chase short limits**, not a PnL arithmetic bug. Tightening TBD after review.

---

## EOD — CCS 7520/7545 showing loss at SPX 7503 (15:00+ CT)

### Operator report

**02-00_C** (`.SPXW260707C7520` / `.SPXW260707C7545`, entry **$0.55** credit, qty 1) still `status: open` at EOD. SPX cash close **~7503** → both calls OTM → spread should expire **$0** → expected PnL **+$55**. Dashboard showed roughly **−$300** (live query showed **−$374** on same settle file).

### Root cause — wrong settlement SPX in `trades/settlement/2026-07-07.json`

| Source | SPX value | Used for settlement? |
|--------|-----------|----------------------|
| **`trades/settlement/2026-07-07.json`** | **7524.29** (`source: manual`) | **Yes — wins first** |
| `data/2026-07-07/SPX_polls.csv` (last ≤ 15:00 CT) | **7500.15** | No (file override exists) |
| MQTT at review time | ~7503 | No (settlement path frozen) |
| `index-ohlc-downloader` daily CSV | No 2026-07-07 row yet | N/A |

`get_spx_settlement_close()` reads the settlement JSON before polls/OHLC. **7524.29 is above the 7520 short strike**, so intrinsic math treats the short call ITM.

### Settlement math at wrong vs correct SPX

**Formula** (`common/expiry_settlement.py`):  
`close_debit = max(0, SPX − short_K) − max(0, SPX − long_K)`  
`pnl = (net_credit − close_debit) × 100 × qty`

| SPX close | Short 7520 intr | Long 7545 intr | Spread debit | PnL (0.55 cr) |
|-----------|-----------------|----------------|--------------|---------------|
| **7524.29** (bad file) | 4.29 | 0.00 | **4.29** | **(0.55 − 4.29) × 100 = −$374** |
| **7503** (operator) | 0.00 | 0.00 | **0.00** | **(0.55 − 0) × 100 = +$55** |
| 7523.55 (hypothetical) | 3.55 | 0.00 | 3.55 | −$300 (matches ~operator −$300 if settle were slightly lower) |

Dashboard `_trade_pnl()` after 15:00 CT calls `compute_settled_pnl()` when `spx_settle` resolves → `pnl_frozen: true`, `cur_spread` set from intrinsic (not live MQTT `breach_watch.spread_mid` of 0.0).

### Fix (operator — no code change required)

Update `trades/settlement/2026-07-07.json` → `"spx_close": 7503` (or official SET print). Refresh dashboard; **02-00_C** should show **+$55** until the trade is archived/closed in JSON.

### Fix applied (code — same session EOD)

`common/expiry_settlement.py` settlement priority after **15:00 CT**:

1. Operator-locked manual (`locked: true`)
2. **`mqtt_settlement`** — first MQTT SPX read at/after 3 PM, persisted to `data/YYYY-MM-DD/spx_mqtt_settlement.json`
3. Daily OHLC CSV
4. SPX polls (fallback before MQTT capture or if streamer unavailable)

Stop supervisor captures MQTT settlement each cycle after 3 PM (once per day). Unlocked stale manual files still ignored when they disagree with trusted sources.

---

## End-of-day sign-off

| Item | Pass / fail | Notes |
|------|-------------|-------|
| Manual spread open + stop | pass | ms-184, ms-185 |
| Manual dashboard close | **fail** → fixed | ms-185 F-9; ms-186 validated ~13:16 |
| MEIC tranche | mixed | Exchange + software breach exits; shared-stop gap on 7535C |
| Software breach slippage | **review** | Tabled — detection OK, execution vs 2× policy gap |
| Stop monitor / streamer | partial | streamer stale episodes; breach wave ~13:49 |
| EOD settlement PnL | **fixed** | Stale manual settle file; polls now win unless `locked` |
| EOD archive clean | | |
