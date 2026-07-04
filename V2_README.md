# MEIC-with-Dash-main-V2

Modular rewrite of the SPX MEIC autotrader. **V1 remains frozen** at `../MEIC-with-Dash-main`.

## V2 differences from V1

| Area | V2 |
|------|-----|
| Trade paths | `trades/active/{MEIC_IC\|MANUAL_SPREAD}/` |
| Ops files | `trades/pause_tranches.json`, `killswitch.json`, `heartbeat.json` |
| Trade JSON | `strategy_version: 2.0`, `spread_type`, `stop_profile` (clean break — no V1 shim) |
| Strategies | `config/strategies.yaml` — MEIC + Manual Spread |
| Blocks | `blocks/` — migration target; runtime still uses ported V1 modules |

Design docs: `changes/V2_MODULAR_REWRITE.md`, `changes/V2_APPENDIX_GAPS.md`.

## Quick start (production)

1. Copy or verify `.env` (credentials from V1).
2. Create venv and install deps:
   ```powershell
   cd MEIC-with-Dash-main-V2
   python -m venv .venv
   .\.venv\Scripts\pip install -r requirements.txt
   ```
3. Start Mosquitto (MQTT broker on localhost:1883).
4. Launch:
   ```powershell
   .\.venv\Scripts\python.exe run.py
   ```
5. Dashboard: http://localhost:5002

Paper mode: `python run.py --paper` or `PAPER_MODE=true` in `.env`.

## Directory layout

```
trades/
  active/MEIC_IC/          # scheduled tranche spreads
  active/MANUAL_SPREAD/    # dashboard manual spreads
  history/{strategy}/      # archived closed trades
  commands/                # per-trade close commands
  pause_tranches.json
  heartbeat.json

blocks/          # modular targets (see blocks/README.md)
brokers/         # BrokerBase + TastyTrade (swappable layer)
blocks/stop/       # post-entry lifecycle (monitor, runner, phases, state)
meic0dte/        # MEIC entry (→ blocks/entry)
streaming/       # DXLink → MQTT (→ blocks/streamer)
dashboard/       # Flask UI
strategies/      # MEIC + ManualSpread registry classes
config/          # strategies.yaml
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -q
```

## Next implementation steps (Phase 2)

1. ~~G9 config validation at startup~~ **Done**
2. ~~Stop block move (`stop_monitor/` → `blocks/stop/`)~~ **Done**
3. Paper-day integration run checklist — see below

## Paper-day integration checklist

Run on a **paper** account during market hours (9:30–15:00 CT). Check each item; note pass/fail and time.

### Pre-open (before 9:30 CT)

- [ ] `.env` credentials valid; `PAPER_MODE=true` or `python run.py --paper`
- [ ] Mosquitto running (`localhost:1883`)
- [ ] `pytest tests/ -q` — all green
- [ ] `python run.py` starts without `StrategyConfigError` (validates `config/strategies.yaml`)
- [ ] Dashboard loads at http://localhost:5002
- [ ] Health panel: streamer dot green within ~30s of market open (or amber if pre-open)

### MEIC scheduled tranches

- [ ] Orchestrator logs tranche slots from `MEICStrategy.schedule()` (not hardcoded list)
- [ ] First tranche fires at configured time; scan finds candidates in credit band
- [ ] Trade JSON written under `trades/active/MEIC_IC/` with `strategy_version: 2.0`, `stop_profile`
- [ ] Stop monitor picks up trade; phases advance (open → monitoring)
- [ ] Dashboard shows active MEIC trade

### Manual spread (dashboard)

- [ ] Manual page: scan candidates visible (high-contrast rows)
- [ ] Select candidate → open handshake completes
- [ ] Trade under `trades/active/MANUAL_SPREAD/`
- [ ] Stop monitor uses same `stop_profile` resolution as MEIC

### Stop / streamer resilience

- [ ] `trades/streamer_health.json` updates every ~5s while streamer runs
- [ ] Pause streamer briefly → stop monitor freezes breach checks (stale SPX >30s), logs warning
- [ ] Restart launcher mid-day with trade in `closing` phase → orphan recovery resumes close (G8)

### Ops controls

- [ ] `trades/pause_tranches.json` skips next tranche when set
- [ ] Killswitch stops new entries
- [ ] Manual close command in `trades/commands/` closes trade cleanly

### End of day

- [ ] After 15:00 CT, launcher/streamer **do not** exit immediately if started after close (after-hours fix)
- [ ] Closed trades archived to `trades/history/{strategy}/`
- [ ] No orphan files left in `trades/active/`

### Sign-off

| Date | Operator | MEIC tranche | Manual open | Stop breach (simulated) | Notes |
|------|----------|--------------|-------------|-------------------------|-------|
|      |          |              |             |                         |       |
