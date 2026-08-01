# V2 building blocks

Production runtime still delegates to legacy modules where noted; new code should import from here.

| Block | Path | Status |
|-------|------|--------|
| Stop profile | `blocks/stop/stop_profile.py` | **Live** |
| Stop runtime | `blocks/stop/` (`monitor.py`, `runner.py`, `phases.py`, …) | **Live** |
| Stop CLI | `blocks/stop/run.py` | **Live** — launcher entry point |
| MEIC stop | `blocks/stop/profiles/meic.py` | **Live** |
| Breach math | `blocks/stop/breach.py` | **Live** |
| Credit entry | `blocks/entry/credit_spread.py` | **Live** — `CreditSpreadEntry` + `CreditEntryConfig` |
| Streamer | `streaming/publish_tastytrade.py` | Port target — health file at `trades/streamer_health.json` |

See `changes/design/V2_MODULAR_REWRITE.md`.
