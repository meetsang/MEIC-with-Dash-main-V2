# Stop Monitor MQTT Cache — Problem & Fix Plan (Plain English)

**For:** Operator review before code change  
**Date:** 2026-07-08  
**Status:** Draft — awaiting operator confirmation  
**Related:** [LIVE_SESSION_2026-07-08.md](LIVE_SESSION_2026-07-08.md), [SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md](SHARED_STOP_PER_TRANCHE_FIX_PLAIN_ENGLISH.md)

This document covers **two independent issues** discovered on Jul 8:

1. **MQTT cache freeze** (Parts 1–6, P0-A / P0-B) — trading risk when prices go stale  
2. **Heartbeat false alarm** (Part 6B, P0-C) — operator confusion only; stop monitor can be healthy while launcher says “missing”

---

## Post-restart verification (12:12 CT relaunch)

Operator restarted `run.py` at **12:12**. Confirmed **MQTT pricing is live again** in stop monitor:

| Trade | `short_mqtt` / `long_mqtt` | `spread_mid` | `status` |
|-------|------------------------------|--------------|----------|
| `12-30_P` (new) | true / true | 1.2 | ok |
| `12-30_C` (new) | true / true | 0.9 | ok |
| `12-00_P` | true / true | 0.4 | ok |
| `ms-189` | true / true | 0.1 | ok |

Stop monitor log: `Breach armed 12-30 C: spread_mid=0.9`, `Breach armed 12-30 P: spread_mid=1.25`.  
`trades/heartbeat.json` fresh (v3, loop #4300+, 4 active trades).

**Note:** `ms-188` still shows `no_prices` from 11:22 — that trade is `finalized_closed`; stale JSON on disk, not active monitoring.

**Heartbeat warnings continued** at 12:18, 12:24, 12:29 despite healthy heartbeat — see **Part 6B** (separate issue).

---

## Part 1 — The problem in one sentence

When the **stop_monitor process’s MQTT subscriber stops receiving updates** (but the process keeps running), the bot **silently uses stale or empty prices** — including a **hardcoded $0.05 long-close floor** — while the **dashboard and manual “Close selected” still look correct** because they use **different price paths**.

---

## Part 2 — What happened today (ms-187, Jul 8 2026)

### Trade outcome

| Field | Value |
|-------|-------|
| Spread | Put credit **7365 / 7340**, qty **2**, entry ~$0.55 credit |
| Short stop | Exchange stop **`481852812`** @ $1.60 — **filled @ $1.65** (worked) |
| Bot long close | **One** STC @ **$0.05** order **`481860872`** — **never filled**, later cancelled |
| Operator | Manual long STC @ **$0.40** |
| Net | **−$140** (2 lots) vs what ~$0.35–0.40 market might have yielded on the long |

### Timeline (CT)

| Time | Event |
|------|-------|
| **08:33:51** | Stop monitor V3 starts; `MqttPriceCache.start()` — MQTT subscribe once |
| **08:33–08:38** | MQTT ingesting live prices (SPX moving 7457–7465 in `market_data` log) |
| **~08:38:56** | Last `market_data` poll with **changing** SPX |
| **08:45:43** | Streamer process exits (`Process lock released streamer`); launcher restarts streamer |
| **08:45:43+** | `market_data` polls freeze at **SPX 7459.0, QQQ 706.56, IWM 294.445** — unchanged for **hours** |
| **09:19+** | Streamer back; publishing live SPX ~744x and options |
| **10:08:20** | Streamer adds ms-187 legs; quotes flowing in streamer log (~0.30–0.35 on 7340P) |
| **10:09:31** | Breach watch: **`missing MQTT`** both legs (`short_mqtt=false`, `long_mqtt=false`) |
| **10:09:33** | Exchange stop placed (uses **entry math + broker**, not live MQTT mids) — **OK** |
| **10:17:40** | Short stopped @ $1.65 → `status=closing` |
| **10:18:01** | Long STC @ **$0.05** (MQTT miss → floor fallback) |
| **10:18:02+** | Log spam `Long close skip re-place at same limit 0.05` (not thousands of broker orders) |
| **~10:45** | Operator closes long @ $0.40 |
| **11:22:53** | ms-188 “Close selected” → **`source=broker_rest`** debit $0.20 — **correct** (REST fallback) |
| **12:09** | Fresh test client: SPX **~7471** live; **running** `market_data` still frozen at **7459.0** |

### Why $0.05 was not “the market”

Automatic long-close uses `_long_leg_mid()` in `blocks/stop/monitor.py`:

```python
long_p = self.prices.get_market_mid(long_sym) or self.prices.get(long_sym)
return float(long_p) if long_p is not None else 0.05   # hard floor when no quote
```

**There is no broker REST step** on this path (unlike “Close selected”).

---

## Part 3 — Why dashboard looked fine (and confused the diagnosis)

Three **separate** MQTT consumers connect to the same Mosquitto broker:

| Process | Implementation | Reconnect? | Today |
|---------|----------------|------------|-------|
| **Dashboard** (`dashboard/server.py`) | Own paho client, `live_prices{}` | **Yes** — retry loop, subscribe in `on_connect` | **Healthy** — PnL ≈ brokerage |
| **Stop monitor** (`common/mqtt_prices.py`) | `MqttPriceCache` singleton per process | **No** | **Frozen ~08:45** — same process since 08:33 |
| **Market data recorder** | Separate `MqttPriceCache` instance | **No** | **Frozen ~08:45** — `SPX_1m.csv` flat at 7459 |

**Close selected** does not use dashboard prices. It writes a command file; stop monitor’s `ManualKillHandler` calls `resolve_spread_close_debit()`:

1. Try stop monitor MQTT cache  
2. **`broker.fetch_option_mids_api()`** (Tasty REST)  
3. Emergency offset from entry credit  
4. Abort if nothing works  

ms-188 today: step 2 saved it (`source=broker_rest`).

**Conclusion:** Dashboard correctness does **not** prove stop monitor MQTT is live.

---

## Part 4 — How MQTT pricing works in stop monitor (read vs ask)

MQTT is **push**, not pull.

1. At startup: `connect` → `subscribe TASTYTRADE/#` → `loop_start()` (background thread).  
2. Each message updates in-memory dict `_prices`.  
3. Every `get()`, `get_spx()`, `get_market_mid()` only **reads that dict** — **no new request to the broker**, **no age check**.

When the subscriber stops ingesting:

- `get_spx()` may still return **old** SPX (ms-187 logged **7459** while live market was **~7443**).  
- `get_market_mid(option)` returns **`None`** for symbols first published after the freeze → long close → **$0.05**.

This is **not** a symbol-format bug. Fresh `MqttPriceCache()` in a new process receives the same topics immediately.

---

## Part 5 — What breaks when the cache is dead

| Feature | Needs option MQTT? | Needs SPX MQTT? | When cache dead |
|---------|-------------------|-----------------|-----------------|
| **Exchange stop** (2× on short @ broker) | No | No | **Still works** (ms-187 stop filled) |
| **Software breach** (2× credit + $0.20) | **Yes** | No | **Disarmed** — `breach_arm_status=waiting_mqtt`, `status=no_prices` |
| **SW Breach column** | **Yes** | No | Shows “missing MQTT” |
| **Auto long close after stop** | **Yes** | No | **$0.05 floor** — no REST fallback |
| **Close selected** | Prefers MQTT | No | **REST fallback** — usually OK |
| **Phase 3 SPX proximity** | No | **Yes** (stale OK) | May use **stale** SPX |
| **`streamer_stale: false`** | — | — | **Misleading** — streamer writes `streamer_health.json` separately |

**Risk profile when cache is dead:** You rely on **broker exchange stops only**; software backup and auto long-close pricing are wrong or off.

---

## Part 6 — Root cause (what we know vs what we infer)

### Proven from logs

- MQTT subscriber in **long-lived** processes stopped updating ~**08:45** (correlates with streamer crash/restart).  
- Streamer and Mosquitto were **fine later**; dashboard and **new** test clients receive live prices.  
- Stop monitor **never** logged MQTT connect/disconnect (no instrumentation).  
- ms-187, ms-188, ms-189 all logged **`missing MQTT`** for breach watch while streamer published those symbols.

### Inferred (likely, not log-proven)

- Streamer exit at 08:45 disrupted subscriber sessions; `MqttPriceCache` has **no `on_disconnect` / reconnect**.  
- paho `loop_start` thread may have entered a zombie state (connected but no messages).  
- **Not** the primary cause: wrong topic prefix or symbol parsing (dashboard + fresh client disprove).

### Code gaps (design defects)

| Gap | File | Impact |
|-----|------|--------|
| No reconnect | `common/mqtt_prices.py` | One bad event → dead for entire session |
| No `last_msg_at` / staleness | `common/mqtt_prices.py` | Stale prices look live |
| Subscribe at startup only, not `on_connect` | `common/mqtt_prices.py` | Fragile vs dashboard pattern |
| No INFO health logging | `common/mqtt_prices.py` | Incidents invisible |
| Long close: MQTT → **$0.05** only | `blocks/stop/monitor.py` `_long_leg_mid()` | ms-187 hurt |
| Manual kill: MQTT → **REST** → emergency | `blocks/stop/v3/quotes.py` | Asymmetric safety |
| `streamer_health.json` ≠ MQTT cache health | `common/streamer_health.py` vs `mqtt_prices.py` | False “streamer OK” |
| Three independent subscribers | dashboard, stop_monitor, market_data | One dies, others live — silent split-brain |
| V3 `resume_long_chase` spam | `blocks/stop/v3/recovery.py` | Log noise; no MARKET escalation at floor |
| Heartbeat “missing” while process runs | `blocks/stop/v3/supervisor.py` + launcher read race | Operator confusion — **see Part 6B** |

---

## Part 6B — Separate issue: Launcher “heartbeat missing” false alarm

**This is not an MQTT problem.** It does not affect pricing, breach watch, or exchange stops. It only affects **operator trust** in launcher terminal alerts.

### The problem in one sentence

The launcher reports **`STOP_MONITOR — heartbeat missing`** even when stop monitor is **running normally** and `trades/heartbeat.json` is **fresh**, because the health check sometimes reads the file **while it is being overwritten**.

### What you saw today

| Time | Launcher message | Actual state |
|------|------------------|--------------|
| 08:35–12:11 (pre-restart) | `heartbeat missing` / `heartbeat stale` every ~5 min | MQTT cache frozen; monitor process still running |
| **12:12** (restart) | `heartbeat missing` at 12:12:21 | Expected — first seconds before first heartbeat write |
| **12:18, 12:24, 12:29** | `heartbeat missing` again | **False alarm** — heartbeat file valid, breach watch armed, stops placing |

The ~5-minute spacing is **`_terminal_warn_once` cooldown** in `run.py` (300s), not repeated stop-monitor crashes.

### Why it happens (mechanism)

**Writer** — `blocks/stop/v3/supervisor.py` `_write_heartbeat()`:

- Runs every **0.25s** (V3 `TARGET_CYCLE_SEC`)
- Opens `trades/heartbeat.json` with `open(path, 'w')` — **truncates immediately**, then `json.dump()`
- **Not atomic** (unlike `streamer_health.json`, which uses `tmp` + `os.replace`)

**Reader** — `common/service_health.py` `check_stop_monitor_health()`:

- Called from launcher main loop every **5 seconds**
- Single `open` + `json.load` — **no retry**
- Any `OSError` or `JSONDecodeError` → message **`heartbeat missing`** (same text for “file absent” and “partial write”)

On Windows, reading during truncate/write often yields empty or partial JSON → parse error → **“missing”** even though the monitor is fine.

**Reproduced in dev:** concurrent read/write stress test → **254 parse failures in 2 seconds** with the current non-atomic write pattern.

### How to tell real death vs false alarm

| Check | False alarm (this bug) | Real stop_monitor down |
|-------|------------------------|-------------------------|
| `trades/heartbeat.json` `ts` | Updates every &lt;1s | Missing or &gt;60s old |
| `loop_count` in heartbeat | Increments | Frozen or absent |
| `stop_monitor.log` | Recent HTTP / breach lines | Silent or process exit |
| Launcher | `STOP_MONITOR exited unexpectedly` **not** shown | Critical restart line |

Today after 12:12: heartbeat fresh, breach armed, stops placed — **false alarm**.

### Relationship to MQTT incident

- Pre-restart: both **MQTT stale** and **heartbeat warnings** appeared — easy to assume one root cause.
- Post-restart: **MQTT fixed by restart**, **heartbeat warnings persisted** — proves they are **independent**.
- Fixing P0-C does **not** replace P0-B; fixing P0-B does **not** silence heartbeat false alarms.

### Proposed fix — P0-C (see Part 8)

Atomic heartbeat write + reader retry + clearer error text. Small change, no trading logic impact.

---

## Part 7 — Immediate operator action (no code)

**Before trusting auto long-close or software breach today:**

1. **Restart** `run.py` launcher **or** at least stop_monitor subprocess (and ideally `market_data` recorder).  
2. After restart, confirm on an open trade JSON:  
   - `breach_watch.short_mqtt` / `long_mqtt` = **true**  
   - `breach_watch.spread_mid` is a real number (not `null`)  
   - `lifecycle.breach_arm_status` moves toward **`armed`** (not `waiting_mqtt`)  
3. Watch `logs/market_data_*.log` — SPX should **move**, not stay pinned at 7459.0.

Until restart: use **Close selected** for discretionary exits (has REST); do not assume auto long-close will price correctly.

---

## Part 8 — Proposed code fixes (for your confirmation)

### P0 — Must fix (incident prevention)

#### P0-A: Long-close quote ladder (match manual kill)

**Goal:** Never place long STC at $0.05 when broker has live quotes.

**Approach:**

- Extract shared helper (or reuse `resolve_spread_close_debit` logic) for **single-leg long mid**:  
  MQTT → `broker.fetch_option_mids_api([long_sym])` → **block + alert** (no silent floor).  
- Wire `_long_leg_mid()` / `_place_long_close_at_mid()` through that helper.  
- Log `long_close_source`: `mqtt` | `broker_rest` | `blocked_no_quote`.

**Files:** `blocks/stop/monitor.py`, possibly `blocks/stop/v3/quotes.py` (shared `resolve_leg_mid()`).

**Tests:** MQTT missing → REST returns mid → limit ≠ 0.05; both missing → no order placed.

---

#### P0-B: `MqttPriceCache` resilience

**Goal:** Subscriber heals itself; stale data is not served as live.

**Approach:**

1. Migrate to `mqtt.Client(CallbackAPIVersion.VERSION2)` (match dashboard).  
2. Subscribe in `on_connect` (not only before `loop_start`).  
3. `on_disconnect` → schedule reconnect with backoff.  
4. Track `last_msg_at` (global + optional per symbol).  
5. `get()` / `get_market_mid()` return **`None`** if last message older than **30s** (configurable).  
6. INFO logs: connect, disconnect, reconnect, subscribe ack; WARNING if stale > 60s.  
7. Optional: expose `cache_health()` for launcher (`trades/mqtt_cache_health.json`).

**Files:** `common/mqtt_prices.py`, `common/service_health.py`, `run.py` (terminal alert).

**Tests:** Unit test staleness; integration test reconnect after simulated disconnect.

---

#### P0-C: Heartbeat atomic write + launcher read retry *(separate from MQTT)*

**Goal:** Stop false `STOP_MONITOR — heartbeat missing` alerts when stop monitor is healthy.

**Approach:**

1. **Atomic write** in `_write_heartbeat()` (V3 supervisor + V2 runner for consistency):
   - Write to `heartbeat.json.tmp`
   - `os.replace(tmp, path)` — same pattern as `common/streamer_health.py` `write_health()`
2. **Reader retry** in `check_stop_monitor_health()`:
   - On `JSONDecodeError` or `OSError`, sleep **50ms** and retry once before failing
   - Split error messages:
     - File not found → `heartbeat file absent`
     - Parse error after retry → `heartbeat unreadable (write race?)`
     - Stale timestamp → existing `heartbeat stale (Ns, loop #N)` (unchanged)
3. **Optional:** log DEBUG on retry (file only, not terminal) for post-mortems.

**Files:**

| File | Change |
|------|--------|
| `blocks/stop/v3/supervisor.py` | Atomic `_write_heartbeat()` |
| `blocks/stop/runner.py` | Same pattern (V2 path) |
| `common/service_health.py` | Retry + distinct error strings |
| `tests/test_service_health.py` | Retry passes on second read; atomic write race test |

**Tests:**

- Fresh heartbeat → `ok`
- Missing file → `absent` message
- Simulated corrupt first read / good second read → `ok` (retry)
- Concurrent write stress → launcher check does not false-fail

**Scope:** ~30 lines; no config flags; safe to ship with or without P0-A/P0-B.

---

### P1 — Should fix (visibility & chase behavior)

#### P1-A: Launcher / dashboard alert when software breach disarmed

- Alert when any open trade has `breach_arm_status == waiting_mqtt` or `breach_watch.status == no_prices` for > N seconds.  
- Dashboard badge on affected rows.

**Files:** `run.py`, `dashboard/server.py`, maybe `breach_watch.py`.

---

#### P1-B: Long-close chase at floor

- When working limit is already at SPX min tick and MQTT/REST mid is above it → escalate to **MARKET** (or cancel + market) without requiring `long_close_attempts >= 10`.  
- Throttle `resume_long_chase` when a working `long_close_order_id` exists (avoid log spam).

**Files:** `blocks/stop/monitor.py`, `blocks/stop/v3/recovery.py`, `blocks/stop/v3/supervisor.py`.

---

### P2 — Consider later (architecture)

#### P2-A: Single MQTT consumer

- One process owns Mosquitto subscription; others read shared file or local socket.  
- Reduces “dashboard fine / stop monitor blind” split-brain.

**Scope:** Larger refactor — defer unless P0-B insufficient in practice.

---

#### P2-B: Unify streamer health with MQTT cache health

- `streamer_stale` should consider stop_monitor cache freshness, not only streamer’s SPX timestamp file.

---

## Part 9 — What we will NOT change (unless you ask)

- Exchange stop placement math (broker-side 2× stop) — worked correctly.  
- Manual kill REST ladder — keep as-is.  
- Dashboard’s separate MQTT client — can stay; P0-B fixes stop_monitor side first.

---

## Part 10 — Test plan (after implementation)

| # | Scenario | Expected |
|---|----------|----------|
| 1 | Normal session, MQTT live | Breach watch shows spread mid; long close uses MQTT; `source=mqtt` |
| 2 | Simulate MQTT cache stale (mock `last_msg_at`) | `get_market_mid` → None; breach disarmed; alert fires |
| 3 | MQTT missing, REST available | Long close uses REST; manual kill `source=broker_rest` unchanged |
| 4 | MQTT + REST missing | Long close **blocked**; critical log; no $0.05 order |
| 5 | Kill streamer, restart streamer | Cache reconnects within N seconds; prices move in `market_data` log |
| 6 | Exchange stop fill → long chase | Limit near live mid, not floor |
| 7 | Launcher health check during heartbeat write | No `heartbeat missing` false alarm (P0-C) |
| 8 | Stop monitor subprocess stopped | `heartbeat stale` or `absent` — real alert |

---

## Part 11 — Operator decisions (please confirm)

Check what you want implemented:

- [ ] **P0-A** — Long-close: MQTT → REST → block (no silent $0.05)  
- [ ] **P0-B** — `MqttPriceCache` reconnect + staleness + logging  
- [ ] **P0-C** — Heartbeat atomic write + launcher retry *(separate issue — Part 6B)*  
- [ ] **P1-A** — Alert when `waiting_mqtt` / `no_prices`  
- [ ] **P1-B** — Floor chase → MARKET + throttle recovery spam  
- [ ] **P2-A** — Single shared MQTT consumer (larger project)  
- [ ] **P2-B** — Tie streamer health to cache health  

**Immediate ops (no code):**

- [x] Restart launcher / stop_monitor **today** — done **12:12 CT**; MQTT confirmed live (see top of doc)  

**Doc only:**

- [ ] Update [LIVE_SESSION_2026-07-08.md](LIVE_SESSION_2026-07-08.md) with link to this plan after approval  

---

## Part 12 — Summary

| Question | Answer |
|----------|--------|
| Was MQTT broken on the wire? | **No** — streamer published; dashboard saw it |
| Did stop monitor read MQTT wrong? | **No** — it stopped **receiving** updates into its cache |
| Is it fixed if we don’t restart? | **No** — same process since 08:33 still frozen at ~12:09 |
| Would a restart fix today? | **Yes** — new subscriber gets live prices immediately |
| Will restart alone prevent recurrence? | **No** — need P0-B reconnect + P0-A REST fallback |
| Is “heartbeat missing” today a dead stop monitor? | **Usually no** — read/write race on `heartbeat.json` (Part 6B) |
| Does P0-C fix trading? | **No** — operator alerts only; independent of MQTT |

---

*Draft for operator sign-off. Reply with which P0/P1/P2 items to implement (P0-C can ship alone).*
