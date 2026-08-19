# Manual Tab — 1-2-1 Butterflies

**Status:** implemented (v1)  
**Not in scope:** RIB / iron butterfly (call spread + put spread)

## Structures

| UI label | Side code | Legs (5-wide example, body 7505) |
|---|---|---|
| Sell Call Butterfly | `SCBF` | SELL 7500C / BUY 2×7505C / SELL 7510C |
| Buy Call Butterfly | `BCBF` | BUY 7500C / SELL 2×7505C / BUY 7510C |
| Sell Put Butterfly | `SPBF` | SELL 7500P / BUY 2×7505P / SELL 7510P |
| Buy Put Butterfly | `BPBF` | BUY 7500P / SELL 2×7505P / BUY 7510P |

## Behavior

- Scan: OTM body strikes, rank by distance from target credit (sell) or max debit (buy).
- Place: `place_multi_leg_order` (4 leg rows, 3 strikes).
- Stop: `hold_to_expiry` — no automated exchange stop; manual close only.
- Session CSV encodes butterflies via `width=bf:<structure_type>`, `short_strike=body`, `long_strike=wing_half`.

## Key files

- `manual_spread/butterfly.py` — leg builder + side codes
- `blocks/entry/butterfly_scan.py` — OTM scan
- `blocks/entry/manual_worker.py` — launcher entry path
- `dashboard/templates/index.html` — Manual tab structure dropdown
