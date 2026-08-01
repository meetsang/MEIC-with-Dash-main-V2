# Changes — documentation index

**North star (read before any new strategy):** [design/STRATEGY_DESIGN_PATTERN.md](design/STRATEGY_DESIGN_PATTERN.md)

**Living changelog:** [CHANGES_SINCE_2026-07-06.md](CHANGES_SINCE_2026-07-06.md) — add a dated section after every ship, fix, or live finding.

---

## Folder layout

| Folder | What goes here |
|---|---|
| [design/](design/) | Architecture, design patterns, component specs, long-term direction |
| [strategies/](strategies/) | Per-strategy plans, research notes, paper-day logs, operator guides |
| [sessions/](sessions/) | Daily live-session observations (`LIVE_SESSION_*`), day-of incidents |
| [fixes/](fixes/) | Point fixes, incident write-ups, phase/release reports, plain-English fix notes |

---

## design/ — architecture & patterns

| Doc | Summary |
|---|---|
| **STRATEGY_DESIGN_PATTERN.md** | **North star** — strategy shapes, reuse table, checklist, incremental backlog |
| MULTI_STRATEGY_ARCHITECTURE_REVIEW_REFINED_V4.md | Long-term V4 architecture lab (separate-repo target) |
| V2_MODULAR_REWRITE.md | Original V2 modular rewrite spec |
| V2_APPENDIX_GAPS.md | Gap analysis appendix for V2 |
| STOP_MONITOR_V3_DESIGN.md | Stop monitor v3 design |
| STOP_MONITOR_V3_OPEN_POINTS_PLAIN_ENGLISH.md | Open points in plain English |
| GAP_ANALYSIS.md | Cross-cutting gap analysis (e.g. GAP-22 round-robin) |
| OPERATIONAL_HARDENING.md | Ops hardening themes |
| PRE_ENTRY_REST_PROBE_HARDENING_DESIGN.md | Pre-entry REST probe design |
| REST_FILL_SYNC_ENTRY_FIX_DESIGN.md | REST fill sync + entry fix design |
| LAUNCHER_CHILD_RESTART_BACKOFF_DESIGN.md | Launcher child restart backoff |
| MARKET_DATA_EXPANDED_WATCH_DESIGN.md | Expanded market-data watch design |
| ENTRY_MONITOR_CSV_OWNERSHIP.md | Session CSV ownership model |
| DASHBOARD_IMPLEMENTATION.md | Dashboard implementation notes |
| RELEASE_RC_STARTUP_REST_COOLDOWN_GATE.md | Startup REST cooldown gate RC |

## strategies/ — strategy plans & research

| Doc | Summary |
|---|---|
| PAPER_TRADING_MULTI_STRATEGY_OVERVIEW.md | Multi-strategy + paper framework overview |
| PAPER_TRADING_MULTI_STRATEGY_TECHNICAL.md | Technical spec (implemented) |
| PAPER_TRADING_MULTI_STRATEGY_FRAMEWORK.md | Original combined framework spec |
| REVERSE_MEIC_EVENT_DAY_RESEARCH.md | Reverse/debit IC for FOMC/news days (research only) |
| MANUAL_STRATEGY.md | Manual spread strategy guide |
| MEIC_to_Tasty_Project_Plan.md | MEIC → Tastytrade migration plan |
| SKYLINE_PAPER_LOG.md | Skyline first paper-day log (2026-07-31) |
| DISCORD_MEIC_SANG.md | Discord / MEIC ops notes |

## sessions/ — live observations

All `LIVE_SESSION_YYYY-MM-DD.md` files, plus selected day-of incident notes.

## fixes/ — incidents & shipped fixes

Stop-monitor incidents, MQTT/cache fixes, phase reports (PHASE1–6), release candidates, and plain-English fix summaries.
