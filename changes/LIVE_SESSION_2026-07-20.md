# Live Session Notes — Jul 20, 2026

**Status:** Morning incident — **STREAMER + STOP_MONITOR crash loop** (observed ~10:04 CT).  
**Related:** [LIVE_SESSION_2026-07-13.md](LIVE_SESSION_2026-07-13.md) (Avast OPENSSL / SSLKEYLOGFILE fix), [PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md](PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md)

---

## Operator question — what is going on in the launcher terminal?

### Short answer

The launcher is **running**, but **streamer** and **stop_monitor** subprocesses **crash immediately** (exit code 1) and the launcher **restarts them every 5 seconds**. Root cause is **TLS certificate verification failure** reaching TastyTrade (`api.tastyworks.com`). The startup REST probe also failed, so **`new_risk_latched=true`** — even if services recovered, **no new MEIC entries would fire** until REST is healthy and the gate is cleared.

The **“SPX price stale (599046s)”** and **“heartbeat stale (loop #43382)”** warnings are **misleading**: those health files were last written on **Jul 13** and were never refreshed because today's processes never stay up long enough.

---

## Day summary (morning)

| Tranche | Side | Entry window | State | Notes |
|---------|------|--------------|-------|-------|
| 11-00 | P/C | 10:59–11:05 | pending | At risk if SSL not fixed before window |
| 12-00 | P/C | 11:59–12:05 | pending | — |
| 12-30 | P/C | 12:29–12:35 | pending | — |
| 01-15 | P/C | 13:14–13:20 | pending | — |
| 01-45 | P/C | 13:44–13:50 | pending | — |
| 02-00 | P/C | 13:59–14:05 | pending | — |

---

## Timeline (CT)

| Time | Event | Source |
|------|-------|--------|
| **10:03:49** | Launcher started (`run.py`, live TastyTrade) | `logs/launcher_2026-07-20_100349.log` |
| **10:03:51** | Streamer, market_data, stop_monitor spawned | launcher log |
| **10:03:54** | Startup REST probe **failed** (`ok=False`, `status=unknown`) | launcher log + `runtime/trading_gate.json` |
| **10:04:01** | **STREAMER + STOP_MONITOR first crash** — restart loop begins | launcher log + terminal |
| **10:04:01** | Stale health warnings: SPX ~599046s, heartbeat loop #43382 | launcher terminal (stale Jul 13 files) |
| **10:05:03** | Second launcher attempt (same pattern) | `logs/launcher_2026-07-20_100503.log` |
| **~10:05** | Direct `publish_tastytrade.py` run reproduces SSL error | manual repro |

---

## Evidence — crash loop

| Check | Result |
|-------|--------|
| Launcher | **Running** — acquires lock, starts dashboard, spawns children |
| Streamer subprocess | **Exits code 1** within ~10s; log shows OAuth session created then immediate `Process lock released streamer` |
| Stop monitor subprocess | **Exits code 1** within ~1s after `Creating TastyTrade OAuth Session` |
| market_data recorder | **Starts** — MQTT connected; may show ladder from brief/local state |
| `runtime/trading_gate.json` | `rest_status=unknown`, `new_risk_latched=true`, `latch_reason=rest_unknown` |
| `rest_detail` | `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate` |
| `trades/streamer_health.json` | Last update **2026-07-13 11:39:56** (not today) |
| `trades/heartbeat.json` | Last update **2026-07-13 11:39:57**, `loop_count=43382` (not today) |
| `last_successful_probe_epoch` | **null** — no successful REST probe this session |

---

## Root cause — SSL certificate verification failure

### Reproduction

Running streamer or stop_monitor directly fails with the same error:

```
httpx.ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
unable to get local issuer certificate (_ssl.c:1028)
```

- Streamer fails when `DXLinkStreamer` requests `/api-quote-tokens` (after publishing an MQTT session banner).
- Stop monitor fails when `TastyTradeBroker` calls `session.validate()` during account bootstrap.
- A bare `httpx.get('https://api.tastyworks.com', verify=certifi.where())` also fails.

`common/win_ssl_env.py` is loaded (SSLKEYLOGFILE sanitized), but that only addresses the **OPENSSL_Applink** Avast pipe issue from Jul 13 — **not** MITM certificate trust.

### Likely cause

**Antivirus HTTPS scanning** (Avast or similar) intercepting TLS with a corporate/root CA that Python's certifi bundle does not trust. This machine last had a healthy streamer/stop_monitor session on **Jul 13**; something in the SSL trust path changed since then (AV update, cert store, or scanning re-enabled).

### What we ruled out

| Hypothesis | Why unlikely |
|------------|--------------|
| Duplicate process locks | Each child acquires and releases its own lock cleanly — not a lock conflict |
| Launcher main loop stall | Launcher is actively restarting children every 5s |
| Weekend / holiday skip | Jul 20 confirmed normal trading day |
| Tranche already missed | Observation at ~10:05 CT — 11-00 window not yet open |

---

## Impact

| Area | Impact |
|------|--------|
| Live SPX / option quotes | **Down** — no DXLink stream |
| Stop protection | **Down** — stop_monitor never reaches V3 supervisor loop |
| New MEIC entries | **Blocked** — `new_risk_latched=true` from failed startup REST probe |
| Dashboard | **Up** (port 5002) but quotes/bot status will be stale |
| market_data CSV | May start but without live streamer quotes, ladder/SPX ticks degrade |

---

## Observations table

| Time (CT) | Event | Notes |
|-----------|-------|-------|
| 10:03 | Launcher up | Live broker mode; session CSV bootstrapped (12 rows) |
| 10:03 | REST probe fail | Gate latched `rest_unknown` |
| 10:04+ | Streamer/stop_monitor crash loop | Exit code 1 every ~5s |
| 10:04 | Stale health warnings | Jul 13 heartbeat/streamer_health — ignore age numbers |
| ~10:05 | Manual repro confirms SSL | Not a launcher-specific bug |

---

## Immediate operator actions

1. **Stop the launcher** (`Ctrl+C` in the terminal running `run.py`) to end the restart storm.
2. **Fix TLS trust** (pick one that matches your setup):
   - **Avast:** Settings → General → HTTPS Scanning → add exclusions for the project folder **and** `.venv\Scripts\python.exe` (or temporarily disable HTTPS scanning to confirm).
   - **Windows:** Ensure system root CAs are current; if AV uses its own root, export that CA and point Python at it via `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE`.
3. **Verify fix** before restarting bot:
   ```powershell
   cd MEIC-with-Dash-main-V2
   uv run python -c "import common.win_ssl_env; import httpx, certifi; r=httpx.get('https://api.tastyworks.com', timeout=10, verify=certifi.where()); print(r.status_code)"
   ```
   Expect **200** or **404** — not an SSL exception.
4. **Restart launcher:** `uv run python run.py`
5. After startup REST probe is **healthy**, on dashboard: **Re-check REST** → **Resume New Entries** if still latched.
6. Confirm streamer stays up: `logs/stream_pub_tt_*.log` should show continuous quotes; `trades/streamer_health.json` timestamp should be **today**.

---

## Follow-up

| Priority | Item | Status |
|----------|------|--------|
| **P0** | Restore TLS trust on this host | **OPEN** — operator / AV config |
| **P1** | Launcher: don't report stale health from prior session when child never wrote fresh heartbeat | OPEN — cosmetic but confusing |
| **P2** | Document Avast HTTPS-scanning exclusion alongside existing SSLKEYLOGFILE note | OPEN |

---

*Observed: Jul 20, 2026 ~10:05 CT. Last successful streamer/stop_monitor health files: Jul 13, 2026.*
