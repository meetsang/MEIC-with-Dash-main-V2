# Live Session Notes — Jul 23, 2026

**Status:** MEIC **11-00 / 12-00 / 12-30 entered** (P + C each). **Manual trade `ms-42_C` queued but not placed** — dashboard shows nothing; no broker order; session CSV stuck `state=entering`.

**Related:** [LIVE_SESSION_2026-07-22.md](LIVE_SESSION_2026-07-22.md), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md)

---

## Operator question — why didn't my manual trade place or show on the dashboard?

### Short answer

The dashboard **accepted** the manual Take Trade and wrote a session row (`ms-42_C`, state **`entering`**). With `run.py` active, placement is **deferred to the entry monitor** — it does not place inline from the dashboard.

The entry monitor **saw** the row but **did not spawn** a worker because the **new-risk REST gate** requires a **fresh probe** (≤60s) for non-tranche (manual) entries. The last probe was the **12-30 MEIC pre-tranche** probe at **12:28:31 CT**. By **12:30:49** that probe was **stale**, so every spawn attempt logged:

`New-risk REST gate blocked entry spawn: rest_probe_stale — no fresh probe on file`

**No broker order was sent.** **No trade JSON** was created under `trades/active/MANUAL_SPREAD/`, so the dashboard manual grid (which reads active JSON only) shows **nothing**.

This is **not** an entry-monitor stall — MEIC tranches fired normally. It is a **gate + probe scheduling gap** for manual entries between MEIC tranche windows.

---

## Day summary — MEIC tranches

| Tranche | Window (CT) | Outcome | Notes |
|---------|-------------|---------|-------|
| **11-00** | 10:59–11:05 | **Entered** | P + C spawned 10:59:00 / 10:59:02 |
| **12-00** | 11:59–12:05 | **Entered** | P + C spawned 11:59:00 / 11:59:02 |
| **12-30** | 12:29–12:35 | **Entered** | P + C spawned 12:29:00 / 12:29:02; fills ~12:29:04 / 12:29:12 |
| 01-15 | 13:14–13:20 | pending | Next scheduled probe ~13:14 |
| 01-45 | 13:44–13:50 | pending | |
| 02-00 | 13:59–14:05 | pending | |

---

## Manual trade — `ms-42_C` (stuck)

### Session CSV (`trades/session/MANUAL_SPREAD_2026-07-23.csv`)

| Field | Value |
|-------|-------|
| slot_key | `ms-42_C` |
| side | Call spread |
| strikes | short **7455** / long **7480** |
| limit_credit | **$0.45** |
| quantity | **3** |
| expiry | `260723` (0DTE) |
| state | **`deleted`** (operator cancelled ~12:40 CT; was stuck `entering`) |
| trade_path | **(empty)** — no JSON handshake |

### What should have happened

1. Dashboard `POST /api/manual_spread/place` (or `/api/session/manual/place`) → `dispatch_manual_place(launcher_active=True)`
2. Row appended to session CSV with `state=entering`
3. Entry monitor `EntryMonitorRunner._should_fire_manual()` → true
4. `try_claim_manual_row()` → `entering` → `placing`
5. `Spawned entry worker for ms-42_C (MANUAL_SPREAD)` in launcher log
6. `manual_worker.run_manual_entry_row()` → broker order → `trades/active/MANUAL_SPREAD/*.json`
7. Dashboard `build_manual_trades()` → `load_dashboard_manual_trades()` reads active JSON

### What actually happened

| Step | Result |
|------|--------|
| Session row written | ✓ `ms-42_C` in CSV (~12:30 CT, file mtime 12:30 PM) |
| Entry monitor tick | ✓ Running (heartbeat ticks >14k, last_tick_ms ~1–3) |
| `_should_fire_manual` | ✓ Would pass (`entering`, `entry_condition=manual`, no `trade_path`) |
| `_gate_allows_spawn` | ✗ **`rest_probe_stale`** from 12:30:49 onward (every ~60s) |
| Worker spawn | ✗ **Never** — no `Spawned entry worker for ms-42_C` in launcher log |
| Broker order | ✗ None |
| Active JSON | ✗ `trades/active/MANUAL_SPREAD/` empty |
| Dashboard manual grid | ✗ Empty (reads JSON, not session `entering` rows) |

---

## Timeline (CT) — manual gate block

| Time | Event | Source |
|------|-------|--------|
| **08:00** | Launcher + dashboard started | `logs/launcher_2026-07-23_080006.log` |
| **08:30** | Streamer, stop monitor V3, entry coordinator started | launcher log |
| **10:59–12:29** | MEIC 11-00 / 12-00 / 12-30 entered normally | launcher log + session CSV |
| **12:28:31** | REST pre-tranche probe **12-30 OK** (228 ms) | launcher log + `runtime/trading_gate.json` |
| **~12:30** | Operator Take Trade → `ms-42_C` appended (`state=entering`) | `MANUAL_SPREAD_2026-07-23.csv` mtime |
| **12:30:49** | First `rest_probe_stale` gate block (probe >60s old) | launcher log |
| **12:31–12:34** | Repeated `rest_probe_stale` every ~60s | launcher log |
| **12:31** | Streamer exit code 1 → auto-restart (unrelated to manual gate) | launcher log |

---

## Root cause

### Code path

Manual spawns call `evaluate_new_risk_gate(require_fresh_probe=True, strategy=MANUAL_SPREAD, tranche_id=None)` in `blocks/entry/runner.py` → `_gate_allows_spawn()`.

With `tranche_id=None`, `common/trading_gate.py` uses the **global** `last_successful_probe_epoch` and requires age ≤ `REST_READY_MAX_AGE_SEC` (default **60**).

MEIC scheduled tranches use **per-tranche** `probes_by_tranche` records from the probe coordinator (`pre_tranche` 30s before window). **Manual entries have no dedicated probe** — they only pass if a probe happened within the last 60 seconds.

Between MEIC windows (e.g. 12:35 → 13:14), manual Take Trade will **always** hit `rest_probe_stale` unless the operator clicks **Re-check REST** on the dashboard (`POST /api/rest-probe`) or waits for the next tranche probe.

### Why dashboard shows nothing

- `build_manual_trades()` → `manual_spread.entry.load_dashboard_manual_trades()` loads **`trades/active/MANUAL_SPREAD/*.json`** and closed-today history only.
- Session CSV rows in `entering` / `placing` are exposed via **`GET /api/session/manual`** but **not** the main manual P&L grid.
- Dashboard place API returns `status: entering` — operator may interpret that as “working” even though no broker action occurred.

### Prior session — same pattern likely

`MANUAL_SPREAD_2026-07-22.csv` has `ms-41_C` still **`entering`** with empty `trade_path` — same failure mode (queued, never spawned).

---

## Evidence

### Launcher log (gate blocks)

```
12:30:49 [LAUNCHER] New-risk REST gate blocked entry spawn: rest_probe_stale — no fresh probe on file — use dashboard Re-check or wait for coordinator
12:31:50 [LAUNCHER] New-risk REST gate blocked entry spawn: rest_probe_stale — ...
12:32:50 [LAUNCHER] New-risk REST gate blocked entry spawn: rest_probe_stale — ...
```

No line matching `Spawned entry worker for ms-42_C`.

### `runtime/trading_gate.json` (at investigation)

- `last_successful_probe_epoch` tied to **12-30** pre-tranche probe
- `probes_by_tranche`: 11-00, 12-00, 12-30 only — **no manual key**

### Entry monitor health (`trades/entry_monitor_health.json`)

- `tick_count` >14k, `last_tick_duration_sec` ~0.002 — **healthy**
- `pending_meic` tracked; **no `pending_manual` field** (observability gap)

---

## Immediate operator workaround

1. **Re-check REST** on dashboard (trading gate banner → **Re-check REST** button) — runs `POST /api/rest-probe`, refreshes `last_successful_probe_epoch`.
2. Within **60 seconds**, entry monitor should claim `ms-42_C` and spawn — watch launcher log for `Spawned entry worker for ms-42_C (MANUAL_SPREAD)`.
3. **Alternative:** wait for **01-15** pre-tranche probe (~**13:14 CT**) — probe coordinator will refresh global epoch; manual row should then fire (if still `entering`).

If `ms-42_C` was unwanted, set row `skip=true` or delete the CSV line before re-check (otherwise it will place on next fresh probe).

---

## Recommended code fixes (not implemented today)

| Priority | Fix | Rationale |
|----------|-----|-----------|
| ~~**P0**~~ | ~~On `dispatch_manual_place`, trigger a dashboard REST probe~~ | **Done ~12:40 CT** — manual spawn no longer requires fresh probe (`blocks/entry/runner.py`) |
| **P1** | Surface `entering` / `placing` session rows in manual dashboard grid with “queued — waiting for REST probe” | Operator visibility |
| **P1** | Log `MANUAL_ENTRY_BLOCKED` with slot_key when gate blocks manual (not just generic spawn warning) | Faster diagnosis |
| **P2** | Add `pending_manual` to `entry_monitor_health.json` heartbeat | Ops monitoring |
| **P2** | Extend `REST_READY_MAX_AGE_SEC` for manual-only path or use `tranche_id='manual'` probe slot | Decouple from MEIC schedule |

---

## Infra notes (non-blocking today)

- Streamer restarted several times (exit code 1) — launcher auto-recovered; MEIC entries unaffected.
- Ladder disabled (`MEIC_SIDE_OPTION_COLLECTION=false`) — MQTT alignment improved vs Jul 22 (see prior session notes).

---

## Files touched this session

| File | Role |
|------|------|
| `trades/session/MANUAL_SPREAD_2026-07-23.csv` | `ms-42_C` stuck `entering` |
| `logs/launcher_2026-07-23_080006.log` | Gate block evidence |
| `runtime/trading_gate.json` | Stale probe state |
| `trades/active/MANUAL_SPREAD/` | Empty — no trade JSON |
