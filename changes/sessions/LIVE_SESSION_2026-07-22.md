# Live Session Notes — Jul 22, 2026

**Status:** **11-00 MEIC entered** (P + C). **Call stop OK**; **put stop not working at broker** — initial exchange stop placed then cancelled by software breach; breach limit **rejected**; position left **unprotected**.

**Related:** [LIVE_SESSION_2026-07-21.md](LIVE_SESSION_2026-07-21.md) (entry coordinator fix), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md)

---

## Operator question — why didn't the 11-00 put get a stop?

### Short answer

The put **did** get an exchange stop initially (`486310097` STOP_LIMIT @ **$2.50**, 10:59:15 CT). **~11 seconds later**, software breach fired (MQTT spread mid **$6.80** vs threshold **$2.10**), the bot **cancelled** that stop and placed a breach **LIMIT** buy-to-close @ **$8.10** (`486310347`). TastyTrade **rejected** that limit. The JSON still shows `active_stop.status=rejected` with `close_only_mode=true`, so the position is **open with no working broker stop**. This is **not** an entry-monitor miss — entry and initial stop placement worked.

---

## Day summary — 11-00 tranche

| Side | Entry | Initial stop | Current stop state | Notes |
|------|-------|--------------|-------------------|-------|
| **C** | Filled 10:59:06 | `486309979` STOP_LIMIT @ $1.45 | **working** | Normal path |
| **P** | Filled 10:59:15 (chase) | `486310097` @ $2.50 → cancelled 10:59:26 | **rejected** LIMIT @ $8.10 | Software breach → rejected close |

| Later tranches | State |
|----------------|-------|
| 12-00, 12-30, 01-15, 01-45 | `pending` |
| 02-00 | `pending`, **paused** in session CSV |

---

## Timeline (CT) — 11-00 put stop chain

| Time | Event | Source |
|------|-------|--------|
| **08:03** | Launcher started | `logs/launcher_2026-07-22_080304.log` |
| **08:30** | Streamer + stop_monitor V3 started | launcher log |
| **10:58:30** | Pre-tranche probe **11-00 OK** (237 ms) | launcher log + `runtime/trading_gate.json` |
| **10:59:00** | `Spawned entry worker for 11-00_P` | launcher log |
| **10:59:02** | `Spawned entry worker for 11-00_C` | launcher log |
| **10:59:03** | Put entry attempt 1 placed (`486309885` @ $1.00 credit) | launcher log |
| **10:59:06** | Call filled; handoff to stop monitor | launcher log |
| **10:59:08** | Call stop **placed** `486309979` @ $1.45 | `11-00_C_*.json` stop_history |
| **10:59:11** | Put attempt 1 cancelled (no fill 5s) | launcher log |
| **10:59:13** | Put chase order `486310063` @ $0.95 | launcher log |
| **10:59:15** | Put **filled**; stop **placed** `486310097` @ **$2.50** | `11-00_P_*.json` stop_history |
| **10:59:25** | Breach armed (10s fill grace ended) | `lifecycle.breach_armed_at` |
| **10:59:26** | Software breach: cancel stop `486310097`, place LIMIT `486310347` @ **$8.10** | stop_history |
| **~10:59:26+** | Broker **rejected** limit `486310347` | `active_stop.status=rejected` |
| **~11:11** | Put still `status=open`, `close_only_mode=true`, breach_watch `spread_mid=6.8` | trade JSON + heartbeat |

---

## Evidence

### Put trade JSON (`11-00_P_20260722T105901.json`)

| Field | Value |
|-------|-------|
| Fill | Short `.SPXW260722P7495` @ $1.35, long `.SPXW260722P7470` @ $0.40, credit **$0.95** |
| `active_stop` | `486310347`, type **LIMIT**, limit **$8.10**, status **`rejected`** |
| `designated_stop_price` | $2.50 |
| `close_mechanism` | `software_breach` |
| `close_only_mode` | **true** |
| `exit_handler` | `breach_phase1_initial_stop` |
| `breach_watch.threshold` | **2.10** (2× credit $1.90 + $0.20 offset) |
| `breach_watch.spread_mid` | **6.80** (status `breached`) |

### Stop history (put)

1. **placed** — `486310097` @ $2.50, reason `initial_short_stop_2x` (10:59:15)
2. **cancelled** — `486310097`, reason `breach_cancel:spread_stop_breach` (10:59:26)
3. **replaced_limit** — `486310347` @ $8.10, reason `spread_stop_breach` (10:59:26)

### Call trade JSON (contrast)

- Stop `486309979` STOP_LIMIT @ $1.45 — **`working`**
- No software breach; `breach_watch.status=ok`, `spread_mid=0.35`

### Infra at incident time

| Check | Result |
|-------|--------|
| Entry monitor | Working — spawns at 10:59:00 / 10:59:02 |
| REST gate | `probes_by_tranche.11-00.ok=true` |
| Streamer | `live`, `last_spx_price_ts` fresh |
| Stop monitor | `heartbeat.json` — 2 active trades, 1 exit job in flight |

---

## Root cause analysis

### 1. Initial stop **was** created

`setup_initial_stop()` ran successfully at 10:59:15. The put had a valid 2× short STOP_LIMIT at **$2.50** (short fill $1.35 × 2 multiplier).

### 2. False / premature software breach (~11s after fill)

After the default **10s fill grace** (`BREACH_FILL_GRACE_SEC`), breach logic saw:

- **Threshold:** $2.10 (`two_x_net_credit` $1.90 + $0.20)
- **MQTT spread mid:** **$6.80** (short mid − long mid)

That implies MQTT short mid near **~$7+** right after a **$1.35** fill — likely **stale or spiked quotes** on the short leg immediately post-fill (put had a **chase** and slower fill than the call). Streamer was not stale (`streamer_stale=false`).

Phase 1 then ran `replace_with_limit_close()`:

- Cancelled working exchange stop `486310097`
- Placed LIMIT buy-to-close at `round_spx_option_price(short_mqtt_mid)` → **$8.10**

### 3. Breach limit **rejected** at broker

The $8.10 limit (vs $1.35 short fill) was rejected by TastyTrade. `active_stop.status` remained **`rejected`** with the order id still on file.

### 4. No exchange stop restored (stuck exit state)

After breach:

- `close_only_mode=true` — normal stop-maintenance path is suppressed in legacy tick
- `exit_action_confirmed()` returns true once `breach_limit_placed_at` is set — **even if the limit was rejected**
- V3 supervisor may keep an **exit job** active (`heartbeat`: `active_exit_jobs=1`), short-circuiting `_ensure_stop_for_filled_qty()` replacement logic
- `stop_is_current()` is false (rejected stop), but replacement did not complete

**Net:** Operator sees “no stop” because the **working** stop was cancelled and the **replacement** failed.

### Ruled out

| Hypothesis | Why unlikely |
|------------|--------------|
| Entry didn't fire | Both sides entered; launcher spawn + fill logs present |
| REST gate blocked entry | Probe OK at 10:58:30 |
| Stop never attempted | stop_history shows placed @ 10:59:15 |
| Streamer down | `streamer_health.json` live |

---

## Comparison — why call was fine

| | Call (C) | Put (P) |
|---|----------|---------|
| Fill time | 10:59:06 | 10:59:15 (chase) |
| Stop placed | 10:59:08 | 10:59:15 |
| Software breach | No | Yes @ 10:59:26 |
| MQTT spread at watch | ~$0.35 (ok) | ~$6.80 (breached) |

Put’s later fill + chase path exposed it to noisier post-fill MQTT on the short leg during the grace window.

---

## Immediate operator actions

1. **Verify broker** — confirm no working stop on `.SPXW260722P7495`; call stop `486309979` should still be live.
2. **Manual protection** — place exchange STOP_LIMIT on put short leg (~$2.50 trigger) **or** close put spread manually if desired.
3. **Do not assume dashboard** — put JSON shows `close_only_mode`; bot may not auto-replace stop until state is cleared.
4. Optional JSON repair (advanced): clear `close_only_mode`, set `active_stop` to null, reset `close_mechanism` — only if you understand v3 recovery; **manual broker stop is safer**.

---

## Follow-up (code / design)

| Priority | Item | Notes |
|----------|------|-------|
| **P0** | On breach LIMIT **rejected**, re-place **exchange** stop (do not leave naked) | `monitor._ensure_stop_for_filled_qty` path blocked by `close_only_mode` / exit job |
| **P0** | `exit_action_confirmed()` should require working/rejected handling, not just `breach_limit_placed_at` | `blocks/stop/v3/recovery.py` |
| **P1** | Extend fill grace or require N confirmations **after** grace when spread_mid >> threshold (e.g. >3×) | Reduce false breach on post-fill quote spikes |
| **P1** | Cap breach limit vs fill (reject absurd MQTT mid before placing) | $8.10 vs $1.35 fill |
| **P2** | Log breach + stop events to launcher session log | Stop monitor events not visible in `launcher_*.log` today |

---

## Session health (~11:11 CT)

| Component | Status |
|-----------|--------|
| Launcher | Running since 08:03 |
| Entry monitor | Working (11-00 spawned on schedule) |
| Streamer | Live, 223 symbols |
| Stop monitor V3 | Running; 2 active trades, 1 exit job |
| REST gate | Healthy |

---

## Operator repair (~11:21 CT)

Manual exchange stop restored after breach-limit reject:

| Field | Value |
|-------|-------|
| Broker order | **`486330276`** STOP_LIMIT BUY_TO_CLOSE `.SPXW260722P7495` |
| Trigger / limit | **$2.50** / **$2.60** (2× short, standard MEIC math) |
| Broker status | **live** (working) |
| JSON | `active_stop` updated; `close_only_mode=false`; exit handler cleared |

---

*Observed: Jul 22, 2026 ~11:00–11:21 CT.*
