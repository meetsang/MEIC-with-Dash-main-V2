# Pending MEIC stop repairs — 2026-07-07 (~13:52 CT)

**Status:** Awaiting operator confirmation before placing at Tasty or updating JSON.

---

## Live broker snapshot (queried ~13:52 CT)

| Short leg | Broker short qty | Live BTC stops | Stop prices |
|-----------|------------------|----------------|-------------|
| `.SPXW260707P7485` | **1** | **1** (`481611982`) | STOP **2.05** / LIMIT **2.15** |
| `.SPXW260707C7535` | **3** | **2** (`481611985`, `481611896`) | **1.60/1.70** and **1.20/1.30** |

---

## Ownership verification (first tranche vs subsequent)

### Put 7485 — **no new stop needed**

| Lot | JSON status | Open order | Stop order | Notes |
|-----|-------------|------------|------------|-------|
| **12-00_P** (1st tranche) | open | `481561770` | **`481611982`** @ 2.05/2.15 | Real broker stop; replaced original `481561791` ~13:49 |
| 12-30_P | **closed** | `481576412` | `481611859` filled | Software breach ~13:49–13:50 |
| 01-45_P | **closed** | `481609237` | `481611917` filled | Software breach ~13:49–13:50 |

Subsequent 7485 puts were reconciled to the first tranche’s shared stop (`481561791`) before breach closed them. **Only 12-00_P remains open** — broker qty (1) matches one working stop.

### Call 7535 — **1 new stop still needed**

| Lot | JSON status | Open order | Stop order | Owner? |
|-----|-------------|------------|------------|--------|
| **12-00_C** (1st tranche) | open | `481561830` | **`481611985`** @ 1.60/1.70 | **REAL** — phase-2 upgrade ~13:49 |
| **12-30_C** | open | `481576413` | **`481611985`** @ 1.60/1.70 | **SHARED** — same order as 12-00_C |
| **01-15_C** | open | `481595000` | **`481611896`** @ 1.20/1.30 | **REAL** — own phase-2 stop |

Original first-tranche call stop was `481561865` @ **2.15/2.25** (cancelled). Subsequent lots (12-30_C, 01-15_C) were wrongly linked via `broker_reconcile` before phase-2 activity.

**Gap:** 3 short calls, 2 live stops → **12-30_C** needs its own 1-lot stop.

---

## Proposed broker order (pending confirm)

### ~~Put~~ — skip

No additional put stop. Position and protection are aligned (1 lot / 1 stop).

### Call — place **one** stop for **12-30_C**

| Field | Value |
|-------|-------|
| Trade JSON | `trades/active/MEIC_IC/12-30_C_20260707T122902.json` |
| Lot | 12-30_C |
| Symbol | `.SPXW260707C7535` |
| Side | BUY_TO_CLOSE (short leg) |
| Qty | **1** |
| Type | STOP_LIMIT |
| STOP trigger | **2.15** |
| LIMIT | **2.25** |

**Price source:** first tranche **initial** call stop (`12-00_C` `initial_short_stop_2x` on `481561865`).

> **Note:** 12-00_C’s **current** live stop is tighter at **1.60/1.70** (phase-2). Using 2.15/2.25 for 12-30_C is looser than 12-00’s active protection. Alternatives if you prefer:
> - Match 12-00 **current**: STOP **1.60** / LIMIT **1.70**
> - 12-30 **own 2×** from fill 0.97: STOP **1.75** / LIMIT **1.85**

**Do not touch:** `481611982` (12-00_P), `481611985` (12-00_C), `481611896` (01-15_C).

---

## JSON updates after placement (not applied yet)

Only `12-30_C_20260707T122902.json` needs a new dedicated stop entry.

Replace shared `active_stop` / append to `stop_history`:

```json
"active_stop": {
  "order_id": "<NEW_ORDER_ID>",
  "type": "STOP_LIMIT",
  "stop_price": 2.15,
  "limit_price": 2.25,
  "phase": 1,
  "status": "working",
  "placed_at": "<ISO_TIMESTAMP>",
  "quantity": 1
},
"stop_history": [
  "... existing entries through 481611985 phase2_upgrade ...",
  {
    "action": "placed",
    "order_id": "<NEW_ORDER_ID>",
    "price": 2.15,
    "phase": 1,
    "reason": "manual_tranche_stop_repair",
    "timestamp": "<ISO_TIMESTAMP>",
    "spx_price_at_event": null
  }
],
"designated_stop_price": 2.15,
"breach_watch": {
  "exchange_stop": 2.15
}
```

Also clear erroneous phase-2 fields on 12-30_C if we place phase-1 priced stop (`phases.phase2_activated_at`, `exit_handler`, etc.) — confirm desired phase state when approving.

---

## Revised count vs original request

| Original ask | Current reality |
|--------------|-----------------|
| +1 put stop (7485) | **0** — 12-30_P and 01-45_P closed via breach |
| +2 call stops (7535) | **1** — 01-15_C already has `481611896`; only 12-30_C orphaned |

---

## Confirm to proceed

Reply **confirm** (and price choice if not 2.15/2.25) to:

1. Place the 12-30_C stop at Tasty
2. Record new order #
3. Update `12-30_C_20260707T122902.json`
