# Test trade fixtures

JSON under `trades/test/` is **excluded** from:

- History tab SQLite sync (`dashboard/history_sync.py`)
- Dashboard manual-trade totals (`manual_spread/entry.py`)
- Production trade JSON scanners (`common/expiry_settlement.py`)

## Layout

| Path | Purpose |
|------|---------|
| `trades/test/active/MANUAL_SPREAD/` | Open test fixtures (`seed_dual_manual_kill_fixture.py` default) |
| `trades/test/history/MANUAL_SPREAD/` | Archived closed test trades |
| `trades/test/active/MEIC_IC/` | MEIC test fixtures (if needed) |
| `trades/test/history/MEIC_IC/` | MEIC test archives |

## Known test lots

`ms-99`, `ms-100`, `ms-v3`, `test-lot`, `test` — also filtered by lot name even if a file lands in production history by mistake.

## Purge SQLite after accidental sync

```powershell
uv run python -c "from dashboard.db import purge_known_test_trades_from_db; print(purge_known_test_trades_from_db())"
```
