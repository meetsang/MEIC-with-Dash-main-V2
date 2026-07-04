MEIC TastyTrade Port -- Gap Analysis
Comprehensive mismatch and gap analysis of the MEIC trading bot port from Schwab to TastyTrade. Covers the Project Plan (Goals 1-5), every gap in SYSTEM_GAPS.md, and newly discovered issues.

1
P0 Total
11
P1 Total
10
P2 Total
22
Total Gaps
Section A: Mismatch Summary (Plan vs Code)
Comparing each planned change from the Project Plan against what was actually implemented in the codebase.

9
Done
6
Deviated
2
Partial
2
Missing
What 'Deviated' means
Deviated items were implemented but with a different approach than planned. This is not necessarily bad -- several deviations are improvements (e.g., BrokerBase abstraction instead of direct SDK calls, per-trade JSON instead of patching order_params.json).
Planned Change	What Was Built	Status
Auth: Replace Schwab OAuth with TastyTrade session

Single session.py with get_session()/get_account(); .env keys TT_USERNAME, TT_PASSWORD

tt_auth.py uses OAuth2 (client_secret + refresh_token) + PaperSession; tt_config.py loads .env. Keys are TT_CLIENT_SECRET, TT_REFRESH_TOKEN, TT_ACCOUNT_NUMBER

Deviated
Auth: Remove 25-min Schwab token refresh

Replace with Tasty validate() keep-alive

run.py still has _token_refresh_loop() but only starts it when BROKER=schwab. TastyTrade session validates on broker init. No periodic re-validation thread for Tasty

Done
Order layer: Replace Schwab REST with SDK

orderdetails.py REPLACE with NewOrder builders; order.py REPLACE with SDK calls

New brokers/tastytrade_broker.py with BrokerBase abstraction. Legacy meic0dte/order/ still exists for Schwab path. TastyTrade path uses broker.place_spread_order(), place_stop_order(), etc.

Done
Fill checking: Map OrderStatus to integer codes

REPLACE check_openfill to call get_live_orders/get_order; return integer codes 0-4

fill_sync.py + broker.get_order_status() returns OrderResult dataclass with string statuses. Integer codes not used; callers use string comparisons

Deviated
Open scan: Replace Schwab REST quotes

DXLink snapshot via mids_for_symbols(); get_option_chain once per tranche

open_spread_tt.py uses MQTT price cache (broker.get_option_price) instead of DXLink snapshot. Chain is fetched per-order inside tastytrade_broker, not once per tranche

Deviated
Open scan: Carry Option objects through pipeline

Return (short_opt, long_opt, spread_credit) from scan

Scan returns string symbols + strikes; Option objects built inside broker on each order call via get_option_chain lookup

Deviated
order_params.json: Add short_streamer, long_streamer, opt_type, short_strike

Non-breaking additions to order_params.json

Per-trade JSON (trades/active/*.json) replaced order_params.json entirely. New schema has short_leg.symbol, short_leg.strike, entry.side, etc. Dashboard compatibility layer unclear

Deviated
Stop placement: 2x short fill, offsets, $2.90 tick rule

Keep stop math; build via new stop_limit_order()

monitor.py setup_initial_stop() uses (short_fill - 0.10) * stop_mult, then round_spx_option_price(). Tick rounding uses $3.00 threshold (option_ticks.py). Stop placed via broker.place_stop_order()

Done
Close/stop engine: Preserve 4-step logic

EDIT (light) — phases stay, only broker call signatures change

Refactored into phases.py plugin system (Phase1/Phase2/Phase3). All 4 steps present. Phase 1 = breach detection + stop monitor. Phase 2 = spread-stop upgrade when long <= $0.05. Phase 3 = SPX proximity close at 14:51

Done
Streaming: Replace schwab-py with DXLinkStreamer

REPLACE publish.py with TastyTrade DXLinkStreamer loop

publish_tastytrade.py implements DXLinkStreamer -> MQTT. Subscribes Quote + Trade on SPX. Dynamic symbol add from optsymbols.json. 3PM stop. Kill switch publish

Done
Streaming: Symbol format in MQTT topics

Publish mid on TOPIC_PREFIX + streamer_symbol; rename prefix to TASTYTRADE/

Uses TASTYTRADE/ prefix via broker_factory.get_mqtt_topic_prefix(). Symbols in TastyTrade format (.SPXW...)

Done
Dashboard: Keep working as-is

KEEP server.py, db.py, templates unchanged

Dashboard still reads order_params.json (legacy path). Per-trade JSON (trades/active/) is the new state. Dashboard may not reflect new architecture without adapter

Partial
Launcher run.py: Keep scheduler, replace token refresh

EDIT minimal — keep TRANCHES, wait_until, subprocess spawns

run.py preserved with TRANCHES schedule. Adds stop_monitor subprocess. Token refresh gated to schwab only. Integration session mode added

Done
requirements.txt: Drop schwab-py, add tastytrade

Drop schwab-py, rauth; add tastytrade pinned

Both schwab-py AND tastytrade present. rauth still listed. tastytrade>=12.4.0 added

Partial
Symbol format: Retire string-built symbols and slicing

Replace create_option_symbol and [-9]/[-7:-3] slicing with Option objects

symbols.py provides parse_canonical/to_tastytrade/to_schwab translation. create_option_symbol still exists in utilities.py. State stores TastyTrade symbols; slicing replaced by stored strike/side fields

Done
Goal 2: Paper mode

PAPER + IS_TEST config; cert account routing; SIM- synthetic ids

PAPER_MODE env var + PaperSession (tastyware API key). No local SIM- dry-run mode. Cert account via TT_IS_TEST. Paper routes through real TastyTrade paper API

Deviated
Goal 3: Remove MQTT -> internal queue

Collapse to single process; replace MQTT with shared_queues

NOT implemented. MQTT still required (paho-mqtt). Multi-process architecture preserved

Missing
Goal 4: Decouple stop engine into own process

Standalone stop_manager.py keyed by stop order id; per-trade JSON; fire-and-forget entry

DONE differently: stop_monitor/ package with MonitorRunner supervisor. Per-trade JSON under trades/active/. Entry thread is fire-and-forget (vertical_thin.py). Stop monitor runs as separate subprocess

Done
Goal 5: Multi-ticker generalization

Instrument registry; instrument-agnostic scan/stop; Strategy protocol

NOT implemented. SPX hardcoded throughout (config.py, open_spread_tt.py, tastytrade_broker.py chain lookup)

Missing
Recommended Fix Order
1. P0: Long close lifecycle (track order, wait for fill, then finalize) -- unblocks trustworthy exit accounting. 2. P1: Session re-validation -- prevents mid-day auth expiry. 3. P1: Decouple fast breach from slow broker sync -- restores legacy-class breach speed. 4. P1: Long leg chase loop -- parity with legacy longclose.py. 5. P1: AlertListener re-registration -- cheap win after order IDs churn. 6. P1: 3 PM admin close broker flatten -- defensive, especially for future non-0DTE. 7. P1: Streamer timezone fix -- critical if ever deployed outside Central timezone. 8. P1: Chain caching -- performance, reduces order latency. 9. P1: Broker retry logic -- resilience against transient API failures. 10. P2: All remaining items (file locks, MQTT events, janitor, etc.)