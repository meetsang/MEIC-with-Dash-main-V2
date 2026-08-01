# Terminal logging — quiet when healthy

**Status:** Implemented (2026-07-07 late)  
**Goal:** Operator terminal shows **microservice problems only**. Full detail stays in per-service log files under `logs/`.

---

## Principle

| Surface | What appears |
|---------|----------------|
| **Terminal (stderr)** | Service **failures / degradation** at a **broad** level — one line per issue, rate-limited |
| **Terminal when healthy** | **Silence** — no poll spam, no PIDs, no per-trade chatter |
| **Log files** | Everything — same INFO detail as before |

---

## Microservices in this repo

| Service | Process | Log file | Terminal |
|---------|---------|----------|----------|
| **Launcher** | `run.py` | `logs/launcher_YYYY-MM-DD_HHMMSS.log` | Failures + explicit operator messages only |
| **Streamer** | `streaming/publish_tastytrade.py` | `logs/stream_pub_tt_*.log` | None (launcher alerts if dead/stale) |
| **Market data** | `python -m market_data.run` | `logs/market_data_*.log` | None (launcher alerts if dead) |
| **Stop monitor** | `blocks/stop/run.py` | `logs/stop_monitor_*.log` | None (launcher alerts if dead/stale heartbeat) |
| **Dashboard** | `dashboard/server.py` | Werkzeug errors only | None at startup |
| **Entry workers** | `meic0dte/app_main.py` (per tranche) | tranche / app logs | None |

---

## What **does** print to terminal

### Always (explicit `terminal_info` or WARNING+)

- Invalid `strategies.yaml` — cannot start
- Non-trading day skip (holiday / FOMC) — one line why bot exited
- **Subprocess died** — `STREAMER`, `STOP_MONITOR`, or `MARKET_DATA` exited → restart attempt
- **Dashboard died** — outer launcher loop
- **Degraded health** (rate-limited ~5 min per issue):
  - Streamer: SPX MQTT price stale > 60s
  - Stop monitor: `trades/heartbeat.json` older than 60s
- Session **fatal** errors (cleanup failed after retries, lock already held)
- Operator interrupt / shutdown errors

### Examples

```
14:02:11 [LAUNCHER] STREAMER exited unexpectedly (code 1) — restarting ...
14:05:22 [LAUNCHER] STREAMER — SPX price stale (92s)
14:49:01 [LAUNCHER] STOP_MONITOR — heartbeat stale (74s, loop #4821)
09:01:00 [LAUNCHER] 2026-07-08 is a normal trading day. Proceeding.
09:01:00 [LAUNCHER] NYSE is CLOSED today (2026-07-04). No trading.
```

---

## What **does not** print to terminal

- Streamer / stop_monitor / market_data **INFO** lines (subscribed symbols, breach watch, polls, option snapshots)
- Launcher routine: PIDs, “Starting …”, “Loaded N strategies”, 3 PM shutdown steps
- MQTT poll payloads (`Poll 13:45:00 — {'SPX': 7503}`)
- Per-trade stop monitor decisions
- Flask request logs (werkzeug → ERROR only)

All of the above remain in the matching **log file**.

---

## Implementation

| Module | Role |
|--------|------|
| `common/logging_config.py` | `setup_session_logging` (launcher), `setup_file_only_logging` (children), `terminal_info()` |
| `common/service_health.py` | Streamer staleness + stop_monitor heartbeat checks |
| `run.py` | Quiet logging; child `stdout/stderr=DEVNULL`; health loop alerts |
| `market_data/recorder.py` | File-only logging |
| `streaming/publish_tastytrade.py` | File-only logging |
| `blocks/stop/run.py` | File-only logging |

---

## Operator workflow

1. **Normal day** — terminal stays blank while services run.
2. **Something wrong** — read the one-line `[LAUNCHER]` alert, then open the service log:
   - Streamer → `logs/stream_pub_tt_*.log`
   - Stops → `logs/stop_monitor_*.log`
   - Index OHLC / option snapshots → `data/YYYY-MM-DD/` (not terminal)
3. **Deep dive** — `logs/launcher_*.log` has full launcher timeline.

---

## Future (not in this change)

- Terminal alert when **broker cooldown** or **429** circuit opens
- Terminal alert when **dashboard** `bot_status.json` ≠ running
- Optional `--verbose` flag to restore old terminal INFO for debugging

---

*Related: [OPERATIONAL_HARDENING.md](OPERATIONAL_HARDENING.md), option snapshots in `market_data/option_snapshots.py`*
