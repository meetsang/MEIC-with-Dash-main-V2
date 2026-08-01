# Change — Entry Monitor Owns Session CSV Updates

**Date:** Jun 26, 2026  
**Status:** Implemented (runner-owned CSV writes + dashboard fallback)  
**Related:** [STALE_PENDING_TRADE_JSON.md](STALE_PENDING_TRADE_JSON.md) (Change 4), [INCIDENT_2026-06-22_STOP_AND_LONG_CLOSE.md](INCIDENT_2026-06-22_STOP_AND_LONG_CLOSE.md)

---

## Executive summary

On **Jun 26, 2026** the **11:00 CT** MEIC tranche opened **PCS 7330/7305** and **CCS 7420/7445** on TastyTrade. Both legs filled, both stops were placed, and both trade JSON files were correct. The dashboard showed **CCS as Open** but **PCS looked empty** (strikes, P&L, stop columns all `–`).

Root cause: **two entry worker threads** (`11-00_P` and `11-00_C`) each loaded the same session CSV, each called `plan.save()` on completion, and the **last writer overwrote** the first — leaving `11-00_P` stuck at `state=entering` with no `trade_path` even though the P JSON was `open`.

**Proposed fix:** Entry workers (MEIC PCS/CCS and manual) **must not write session CSV**. After fill (or failure), they **return a result** (including `trade_path` / JSON path) to **Entry Monitor**, which alone updates `trades/session/{strategy}_{date}.csv` — serialized, reload-before-write.

---

## Production incident (Jun 26, 2026 — 11-00 tranche)

### What TastyTrade showed

| Leg | Strikes | Entry order | Stop order |
|-----|---------|-------------|------------|
| PCS | 7330 / 7305 | 479157086 | 479157208 |
| CCS | 7420 / 7445 | 479157101 | 479157207 |

### What local JSON showed (correct)

| File | Status | `open_order_id` | `active_stop.order_id` |
|------|--------|-----------------|------------------------|
| `trades/active/MEIC_IC/11-00_P_20260626T105904.json` | `open` | 479157086 | 479157208 |
| `trades/active/MEIC_IC/11-00_C_20260626T105905.json` | `open` | 479157101 | 479157207 |

Stop monitor heartbeat: `active_trades: 2`.

### What session CSV showed (broken for P)

| slot_key | state | trade_path |
|----------|-------|------------|
| `11-00_P` | **entering** | *(empty)* |
| `11-00_C` | entered | `…/11-00_C_20260626T105905.json` |

Launcher log confirmed **both** workers logged full fill + handoff:

```
10:59:12  Order 479157086 (P) filled 1/1 — handoff to stop monitor
10:59:14  Order 479157101 (C) filled 1/1 — handoff to stop monitor
```

### Manual recovery applied

Updated `trades/session/MEIC_IC_2026-06-26.csv` row `11-00_P`:

- `state` → `entered`
- `trade_path` → `…/11-00_P_20260626T105904.json`

Dashboard then showed both legs as **Open** with strikes and stop IDs.

---

## Where data lives today

| Data | Session CSV | Trade JSON | Notes |
|------|-------------|------------|-------|
| Plan (window, qty, chase, stop×) | ✅ | copied to `plan` on place | CSV is plan source |
| Slot lifecycle (`pending` → `entering` → `entered` / `failed`) | ✅ | — | Dashboard reads CSV first for display state |
| Link row → trade | `trade_path` | file path | Empty `trade_path` breaks primary lookup |
| Entry order id | ❌ | `open_order_id` | Dashboard reads JSON for grid overlay |
| Stop order id | ❌ | `active_stop.order_id` | Stop monitor + dashboard |
| Strikes, fill credit, P&L | ❌ | `short_leg`, `long_leg`, `entry` | Dashboard merges JSON when row resolves |

**Order numbers are not stored in CSV** — by design. CSV holds the pointer (`trade_path`); JSON is the trade ledger.

---

## Shortcomings identified

### 1. Lost CSV update (P0) — concurrent worker `plan.save()`

**Current behavior**

```
EntryMonitorRunner._tick_plan()
  → plan.update_row(P, state=entering); plan.save()
  → plan.update_row(C, state=entering); plan.save()
  → spawn thread P → run_meic_entry_row(plan, row)  → _mark_plan_row → plan.save()
  → spawn thread C → run_meic_entry_row(plan, row)  → _mark_plan_row → plan.save()
```

Each worker holds an **in-memory `SessionPlan` snapshot** from thread start. When P finishes, it saves `P=entered`. When C finishes 2s later, its snapshot still has `P=entering` → **C’s save clobbers P’s update**.

**Affects:** Every tranche where **P and C fire in the same window** (all six MEIC slots). Whichever side fills last “wins”; the other side may appear stuck at `entering`.

**Same pattern in:** `blocks/entry/meic_worker.py`, `blocks/entry/manual_worker.py` (`_mark_plan_row`).

---

### 2. Dashboard hides trade data when CSV says `entering` (P1)

`renderGrid()` only populates strikes/P&L/stop columns when:

```javascript
const hasData = ['open','closing','breached'].includes(st) || st === 'closed' || st === 'killed';
```

`_session_display_state()` returns `entering` **before** checking trade JSON:

```python
if row.state == 'entering':
    return 'entering'
if trade:
    return _slot_state_from_trade(...)
```

So when CSV is stale (`entering`) but JSON is `open`, the grid shows **Entering** with **empty columns** — even though `_resolve_trade_for_row()` can find the JSON via lot+side fallback.

**Operator impact:** Looks like PCS “never registered” while CCS looks fine (depending on which side saved last).

---

### 3. Parallel P/C API pressure (P2)

Both workers started within ~2s (`10:59:03` spawn P, `10:59:03` spawn C). Logs show **429 Too Many Requests** during scan/quote. Fills still succeeded but retries amplify race timing and broker load.

**Mitigation options (later):** stagger C spawn after P handshake, shared quote cache, or sequential entry within tranche (product decision).

---

### 4. No reconciliation loop (P2)

Nothing today re-syncs CSV from JSON if:

- CSV says `entering` but JSON is `open` + filled
- CSV has empty `trade_path` but matching JSON exists in `trades/active/`

Stop monitor runs fine from JSON; only **dashboard + plan UI** suffer.

---

## Proposed solution — Entry Monitor owns CSV writes

### Principle

| Component | Responsibility |
|-----------|----------------|
| **Entry worker thread** | Scan, place, poll fill, write/update **trade JSON**, register streamer symbols, return **result dict** |
| **Entry Monitor (runner)** | Spawn workers, set `entering`, **sole writer** of session CSV for entry outcomes |
| **Stop monitor** | Unchanged — reads JSON from `trades/active/` |

Workers **pass the JSON path (and outcome) back** to Entry Monitor after fill or terminal failure. Entry Monitor applies **one row update** per completion under a **CSV write lock**, with **reload-from-disk** before patch.

---

### Result object (worker → monitor)

```python
@dataclass
class EntryWorkerResult:
    slot_key: str
    status: str          # 'entered' | 'failed' | 'working' (unfilled left working — rare for MEIC)
    trade_path: str      # '' if no JSON
    order_id: str        # optional; for logging only (not persisted to CSV)
    filled_quantity: int
    error: str           # if failed
```

MEIC worker signature becomes:

```python
def run_meic_entry_row(row: SessionRow, row_log) -> EntryWorkerResult:
    ...
    # NO plan.save() / NO _mark_plan_row inside worker
```

Manual worker: same pattern.

---

### Entry Monitor apply path

```python
def _run_worker(self, plan_path: str, slot_key: str, manual: bool) -> None:
    try:
        row = ...  # load row once for worker input
        if manual:
            result = run_manual_entry_row(row, row_log)
        else:
            result = run_meic_entry_row(row, row_log)
    finally:
        with self._lock:
            self._handles.pop(slot_key, None)
            if result is not None:
                self._apply_entry_result(plan_path, result, manual)

def _apply_entry_result(self, plan_path, result: EntryWorkerResult, manual: bool) -> None:
    with self._csv_lock:                    # dedicated lock for all session CSV writes
        plan = SessionPlan.load(plan_path)  # reload fresh from disk
        fields = {'state': result.status}
        if result.trade_path:
            fields['trade_path'] = result.trade_path
        plan.update_row(result.slot_key, **fields)
        plan.save()
        self.log.info('CSV updated %s → %s path=%s', result.slot_key, result.status, result.trade_path)
```

**Also move** `state=entering` save in `_tick_plan` under the same `_csv_lock` (or inline reload-before-save there too).

---

### Sequence (11-00 tranche, fixed)

```
Monitor:  CSV P=entering, C=entering     (one locked save)
Monitor:  spawn P thread, spawn C thread
P worker: place → JSON → fill → return {slot_key: 11-00_P, status: entered, trade_path: …P…json}
Monitor:  reload CSV → patch P row → save   (locked)
C worker: place → JSON → fill → return {slot_key: 11-00_C, …}
Monitor:  reload CSV → patch C row → save   (locked; P row preserved)
```

---

## Secondary fixes (recommended with or after P0)

### A. Dashboard defensive display (P1)

In `_session_display_state()`:

```python
if row.state == 'entering' and trade and trade.get('status') in ('open', 'closing', 'pending_fill'):
    return _slot_state_from_trade(trade.get('status'), ...)
```

Or treat `entering` + resolved JSON as `hasData` in `renderGrid()`.

**Why:** Protects operators when CSV is wrong but JSON is right (today’s incident, partial deploys, manual edits).

### B. Startup reconciliation (P2)

On Entry Monitor tick (or launcher start), for each CSV row with `state in (entering, entered)` and empty/missing `trade_path`:

- Find active JSON matching `lot` + `side`
- If JSON `status == open` and `filled_quantity > 0`, patch CSV → `entered` + `trade_path`

Log: `Reconciled 11-00_P from JSON …`

### C. Tranche stagger (P2)

Optional: delay C worker spawn by N seconds after P, or run P then C sequentially per lot when `entry_window` is shared — reduces 429s and makes debugging easier. Not a substitute for CSV ownership.

---

## Files to change (implementation checklist)

| File | Change |
|------|--------|
| `blocks/entry/runner.py` | `_csv_lock`, `_apply_entry_result`, workers return result; monitor saves CSV |
| `blocks/entry/meic_worker.py` | Remove `_mark_plan_row` / `plan.save`; return `EntryWorkerResult` |
| `blocks/entry/manual_worker.py` | Same |
| `blocks/session/plan.py` | Optional: `patch_row_on_disk(path, slot_key, **fields)` helper (reload + update + save) |
| `dashboard/server.py` | `_session_display_state` fallback when JSON exists (secondary) |
| `dashboard/templates/index.html` | Optional: `hasData` includes `entering` when strikes present (secondary) |
| `tests/test_entry_runner.py` | **New:** concurrent P+C completion → both rows `entered` with correct paths |
| `tests/test_session_plan.py` | Patch-on-disk / lock behavior |

---

## Test plan

1. **Unit:** Two threads call `_apply_entry_result` for different slot_keys on same CSV → both rows persisted.
2. **Unit:** Second apply does not revert first row’s `state` / `trade_path`.
3. **Integration (paper):** Fire 11-00 P+C in same window → dashboard shows both Open without manual CSV edit.
4. **Regression:** Failed entry still sets `state=failed`; pending rows unchanged.
5. **Manual spread:** Place from dashboard → CSV updated via monitor path (dashboard place API may still append row — align with same patch helper).

---

## Operator runbook (until implemented)

If one side shows **Entering** with blank columns but TastyTrade has the position:

1. Check `trades/active/MEIC_IC/*_{lot}_{side}_*.json` — confirm `status: open`, note path and order ids.
2. Open `trades/session/MEIC_IC_{date}.csv` — find `{lot}_P` / `{lot}_C` row.
3. Set `state=entered` and `trade_path=<full path to JSON>` for the affected row(s).
4. Refresh dashboard.

Do **not** delete JSON — stop monitor depends on it.

---

## Acceptance criteria

- [ ] No entry worker calls `SessionPlan.save()` for MEIC or manual entry completion
- [ ] Concurrent P+C fills in one tranche → both CSV rows `entered` with correct `trade_path`
- [ ] Order ids remain in JSON only; dashboard resolves them via `trade_path`
- [ ] Dashboard shows both legs after fill without manual CSV edit
- [ ] Reconciliation (optional) heals stale `entering` rows on next monitor tick

**Implemented Jun 26, 2026:** `blocks/session/csv_update.py`, worker `EntryWorkerResult`, runner applies CSV; dashboard display fallback when JSON exists.

---

## References

- Runner spawn + worker save today: `blocks/entry/runner.py`, `meic_worker._mark_plan_row`
- Dashboard resolve + display: `dashboard/server.py` (`_resolve_trade_for_row`, `_session_display_state`), `index.html` `renderGrid()`
- Jun 26 CSV (post manual fix): `trades/session/MEIC_IC_2026-06-26.csv`
- Jun 26 JSON: `trades/active/MEIC_IC/11-00_P_20260626T105904.json`, `11-00_C_20260626T105905.json`
