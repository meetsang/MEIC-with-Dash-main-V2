# Multi-Strategy Architecture Review — Refined V4 Design

**Date:** 2026-07-05  
**Status:** V4 design roadmap for a separate repo/prototype. Do **not** use this document to refactor the live V3 stop monitor directly.  
**Source base:** `meetsang/MEIC-with-Dash-main-V2`, Claude's original `MULTI_STRATEGY_ARCHITECTURE_REVIEW.md`, the earlier enhanced review, and the implemented Stop Monitor V3 design/review notes.  
**Primary decision:** Keep **V3** as the live exit-safety system. Build **V4** separately as the future multi-strategy platform. Live V3 findings should be fixed in V3 first, then converted into V4 architecture rules and regression tests.

---

## 0. Executive summary

The current MEIC V2/V3 codebase now has a much stronger exit foundation than when the first architecture review was written. Stop Monitor V3 introduced the right production-grade exit concepts:

- feature flag / rollback path
- round-robin `StopSupervisor`
- `TradeSlot` cache
- `ExitWorkerPool`
- handler-based exits
- command claiming
- atomic state writes
- persisted `close_only_mode`
- restart recovery for manual kill
- bounded broker lane
- V2.9 fill-accounting fix

Those V3 elements should **not be removed from the architecture review**. They should be marked as **implemented live baseline** and reused as design lessons for V4.

The next architecture target is:

```text
Market data / signals
  -> StrategyGate
  -> TradeIntent
  -> EntryHandler / ExecutionEngine
  -> PositionState
  -> ExitPolicy / ExitHandler
  -> BrokerAdapter
```

The key rule is:

```text
Strategies decide WHAT to trade.
Entry handlers decide HOW to construct and submit orders.
PositionState records WHAT exists now.
Exit handlers decide HOW that position exits.
BrokerAdapter only translates generic order intent into broker API calls.
```

V4 should prove this architecture by first running **current MEIC credit spreads through the generic contracts in simulation**. Only after that should debit spreads, multi-leg options, signal-driven strategies, or futures be added.

---

## 1. V3 vs V4 separation

### 1.1 V3 is the live safety system

V3 should remain focused on live MEIC/manual SPX/SPXW credit-spread exits.

Allowed V3 changes:

```text
- live bug fixes
- restart/recovery fixes
- duplicate-close protection
- broker/rate-limit fixes
- command-claiming fixes
- fill-accounting fixes
- heartbeat/logging/observability improvements
- tests reproducing actual V3 incidents
```

Avoid in V3 while it is live:

```text
- debit spread architecture
- futures support
- generic order model refactor
- generic position model refactor
- strategy-loader rewrites
- major broker interface redesigns not required for live exit safety
```

### 1.2 V4 is the architecture lab

V4 should be developed in a separate repo or branch with no live broker order placement at first.

Initial V4 constraints:

```text
- no live broker credentials required
- fake broker first
- no production order placement
- no production dashboard dependency
- deterministic tests before broker adapters
- read current V3 trade JSONs for compatibility
```

### 1.3 Fix flow between V3 and V4

If V3 reveals a live issue:

```text
1. Fix V3 first.
2. Add a V3 regression test.
3. Convert the lesson into a V4 architecture rule.
4. Add a V4 compatibility/regression test.
```

Example:

```text
V3 incident:
manual kill command persisted close_only_mode=true, then stop_monitor restarted before status=closing.

V3 fix:
open + close_only_mode re-enqueues ManualKillHandler.

V4 rule:
any persisted exit ownership state must resume the matching ExitPolicy after restart, not merely suppress scanning.
```

---

## 2. What is already implemented in V3 and should be treated as baseline

The earlier architecture review treated the exit engine as a design target. That is now partially implemented in V3. V4 should not duplicate the old V2 stop monitor assumptions.

### 2.1 Implemented / carried forward from V3

| Area | V3 status | V4 lesson |
|---|---:|---|
| `STOP_MONITOR_ENGINE` flag | Implemented | Every risky subsystem needs a rollback/feature flag. |
| V2.9 `long_close_price` fallback | Implemented | Accounting fallbacks must be explicit and strategy-aware. |
| `StopSupervisor` | Implemented | Use one supervisor loop, not one idle thread per trade. |
| `TradeSlot` cache | Implemented | Hot path must not parse JSON every scan. |
| mtime-gated state merge | Implemented after review fix | Position state cache needs invalidation rules. |
| command claiming | Implemented | Operator commands need atomic ownership. |
| manual kill handler | Implemented | Manual operator intent overrides automated breach logic. |
| close-only mode | Implemented/persisted | Exit ownership must survive restart. |
| manual-kill restart recovery | Fixed after review | Persisted exit states must resume work, not strand trades. |
| broker lane | Implemented but still under live observation | Broker concurrency must be bounded and measured. |
| one active exit job per path | Implemented | Duplicate close prevention is mandatory. |
| stuck-job policy | Implemented/design-covered | Exit systems need progress heartbeat. |

### 2.2 Still under live observation in V3

These should not block V4 design, but V4 must keep hooks for them:

| ID | Topic | V4 implication |
|---|---|---|
| T-1 | Real broker parallelism under exit load | `BrokerAdapter` must expose in-flight metrics and rate-limit state. |
| T-2 | Supervisor-thread broker calls | V4 supervisor should never perform broker I/O directly. |
| T-3 | Option chain cache mutex/pre-warm | V4 instrument services need cache-warming and singleflight/mutex behavior. |
| T-4 | True manual-kill priority queue | V4 worker pool should use a real priority queue from the beginning. |
| T-5 | Breach handler restart recovery | V4 exit policies must resume all persisted exit handlers, not only manual close. |

---

## 3. Core problem in the current codebase

The current repo has a modular-looking shell:

```text
strategies/
  base.py
  loader.py
  validate.py
  meic/
  manual_spread/
  iron_fly/
config/strategies.yaml
blocks/entry/
blocks/stop/
brokers/
market_data/
```

But production internals remain shaped around:

```text
SPX / SPXW
0DTE index options
credit verticals / iron-condor-like MEIC lots
short_leg + long_leg JSON
net_credit / two_x_net_credit
SPX option tick rules
SPX phase-3 proximity logic
TastyTrade option-spread order methods
```

That is fine for MEIC V3. It is not enough for:

```text
- debit spreads
- long option strategies
- iron flies / butterflies / custom multi-leg structures
- SPY/QQQ/IWM options
- futures positions
- signal-driven directional entries
- multiple strategies sharing risk budget
```

V4 should not patch these one by one. It should define generic contracts and then adapt MEIC into them.

---

## 4. Non-negotiable V4 design rules

### Rule 1 — MEIC compatibility comes before new strategies

Before V4 supports debit spreads or futures, it must prove:

```text
V3 MEIC trade JSON
  -> PositionState
  -> ExitPolicy selection
  -> simulated manual kill / stop-filled / breach
  -> same intended broker actions as V3
```

### Rule 2 — No live order placement at first

V4 starts with fake broker tests. Live broker adapter comes later.

### Rule 3 — Strategy code never places orders directly

A strategy emits a `TradeIntent`. Execution code converts that into broker orders.

### Rule 4 — Exit code reads `PositionState`, not strategy internals

Exit policies must not depend on MEIC tranche names or dashboard row names. They should depend on:

```text
asset_class
structure
premium_type
legs
entry price/cost/credit
exit policy id
risk policy id
broker order ids
```

### Rule 5 — One active exit owner per position

V3 proved this is critical. V4 should build it into the core `ExitEngine`.

### Rule 6 — Persisted exit ownership must resume after restart

Any position with `exit_state.status in ('accepted', 'working', 'stalled')` must resume the correct handler after restart.

### Rule 7 — Broker I/O is always bounded, observable, and outside supervisor loops

Supervisors scan and dispatch. Workers call broker adapters through lanes/semaphores/rate limiters.

---

## 5. Core contracts to add in V4

### 5.1 InstrumentSpec

```python
@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str                    # SPX, SPY, QQQ, MES, NQ
    asset_class: str               # index_option, equity_option, future, future_option
    broker: str                    # tastytrade, schwab, ibkr later
    broker_underlying: str         # SPX, SPY, /MES, etc.
    option_root: str | None = None # SPXW, SPY, QQQ
    multiplier: float = 100.0
    tick_size: float | None = None
    tick_value: float | None = None
    strike_step: float | None = None
    session_id: str | None = None
    quote_symbol: str | None = None
```

Examples:

```python
SPXW = InstrumentSpec(
    symbol='SPX',
    asset_class='index_option',
    broker='tastytrade',
    broker_underlying='SPX',
    option_root='SPXW',
    multiplier=100,
    strike_step=5,
    session_id='US_INDEX_OPTIONS',
    quote_symbol='SPX',
)

MES = InstrumentSpec(
    symbol='MES',
    asset_class='future',
    broker='tastytrade',
    broker_underlying='/MES',
    multiplier=5,
    tick_size=0.25,
    tick_value=1.25,
    session_id='CME_EQUITY_INDEX',
    quote_symbol='MES',
)
```

### 5.2 TickRules

Replace SPX-only tick functions with instrument-aware rules.

```python
class TickRules(Protocol):
    def tick_for_price(self, price: float) -> float: ...
    def round_limit(self, price: float, side: str) -> float: ...
```

Examples:

```text
SPXW option premium < $3.00 -> $0.05 tick
SPXW option premium >= $3.00 -> $0.10 tick
SPY/QQQ option premium -> usually $0.01 tick
MES future price -> 0.25 index points
```

V4 should not call `round_spx_option_price()` directly except inside an SPX compatibility adapter.

### 5.3 SymbolCodec

```python
@dataclass(frozen=True)
class InstrumentSymbol:
    underlying: str
    asset_class: str
    expiry: str | None = None
    right: str | None = None       # C/P
    strike: float | None = None
    contract_month: str | None = None
    broker_symbol: str | None = None

class SymbolCodec(Protocol):
    def parse(self, raw: str) -> InstrumentSymbol: ...
    def format(self, sym: InstrumentSymbol, broker: str) -> str: ...
```

Current SPXW parsing becomes one codec, not the global symbol model.

### 5.4 OrderLeg and OrderIntent

```python
@dataclass
class OrderLeg:
    symbol: str
    action: str        # BUY_TO_OPEN, SELL_TO_OPEN, BUY_TO_CLOSE, SELL_TO_CLOSE, BUY, SELL
    quantity: int
    ratio: int = 1
    role: str = ''     # short, hedge, long, future, wing, body

@dataclass
class OrderIntent:
    intent_id: str
    strategy_id: str
    position_id: str | None
    instrument: str
    asset_class: str
    structure: str
    order_type: str       # limit, market, stop, stop_limit, bracket
    price_effect: str     # credit, debit, flat, unknown
    limit_price: float | None
    stop_price: float | None
    legs: list[OrderLeg]
    time_in_force: str = 'DAY'
    metadata: dict = field(default_factory=dict)
```

### 5.5 TradeIntent

Strategies emit `TradeIntent`. They do not construct broker calls directly.

```python
@dataclass
class TradeIntent:
    strategy_id: str
    instrument: str
    asset_class: str
    structure: str              # credit_vertical, debit_vertical, iron_condor, future_directional
    direction: str              # bullish, bearish, neutral, long, short
    quantity: int
    constraints: dict           # max_debit, min_credit, width, delta, expiry, time window
    entry_policy_id: str
    exit_policy_id: str
    risk_policy_id: str
    signal_snapshot_id: str | None = None
    metadata: dict = field(default_factory=dict)
```

### 5.6 PositionState

V4 should not force every position into `short_leg` / `long_leg` fields. Use generic legs.

```python
@dataclass
class PositionLeg:
    leg_id: str
    symbol: str
    asset_class: str
    action_open: str
    action_close: str
    quantity: int
    fill_price: float | None
    current_price: float | None = None
    role: str = ''              # short, long, hedge, body, wing, future
    strike: float | None = None
    right: str | None = None
    expiry: str | None = None

@dataclass
class PositionState:
    position_id: str
    strategy_id: str
    instrument: str
    asset_class: str
    structure: str
    premium_type: str | None    # credit, debit, none
    status: str                 # pending, open, closing, closed, cancelled, error
    quantity: int
    opened_at: str | None
    closed_at: str | None
    legs: list[PositionLeg]
    entry: dict
    risk: dict
    exit_policy_id: str
    exit_state: dict
    broker_state: dict
    source_schema: str = 'v4'
```

### 5.7 ExitState

V3 lessons should become standard.

```python
@dataclass
class ExitState:
    close_only_mode: bool = False
    exit_handler: str | None = None
    exit_started_at: str | None = None
    exit_last_step: str | None = None
    exit_last_progress_at: str | None = None
    exit_attempt: int = 0
    exit_stalled: bool = False
    exit_error: str | None = None
    working_order_ids: list[str] = field(default_factory=list)
```

### 5.8 ExitPolicy and ExitHandler

```python
class ExitPolicy(Protocol):
    policy_id: str
    supported_structures: set[str]

    def detect_exit_condition(self, position: PositionState, market: MarketSnapshot) -> ExitDecision | None:
        ...

    def build_exit_job(self, position: PositionState, decision: ExitDecision) -> ExitJob:
        ...

class ExitHandler(Protocol):
    def run(self, job: ExitJob, broker: BrokerAdapter, state_store: PositionStore) -> ExitResult:
        ...
```

Initial policies:

```text
CreditVerticalMeicExitPolicy
ManualCloseExitPolicy
StopFilledLongChasePolicy
DebitVerticalLossExitPolicy
FuturesBracketExitPolicy later
```

### 5.9 BrokerAdapter

```python
class BrokerAdapter(Protocol):
    def place_order(self, intent: OrderIntent) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> CancelResult: ...
    def get_order_status(self, order_id: str) -> OrderStatus: ...
    def get_quote(self, symbol: str) -> Quote: ...
    def get_position(self, broker_position_id: str) -> BrokerPosition: ...
    def close_position(self, position: PositionState, limit_policy: dict) -> OrderResult: ...
```

Broker-specific helpers can exist, but strategy and exit code should target `OrderIntent` and `PositionState`.

---

## 6. V3 compatibility adapter

V4 must include a compatibility adapter before any new strategy work.

### 6.1 Adapter responsibilities

```text
Read existing V3 MEIC trade JSON
  -> infer instrument = SPX/SPXW
  -> infer structure = credit_vertical or credit_spread
  -> map short_leg / long_leg into PositionLeg[]
  -> map net_credit / two_x_net_credit into risk fields
  -> map active_stop / spread_close_order_id into broker_state
  -> map close_only_mode / exit_handler into ExitState
```

### 6.2 Compatibility tests

Required tests:

```text
- V3 open credit spread JSON -> PositionState
- V3 manual_close + close_only_mode + status=open -> resumes ManualCloseExitPolicy
- V3 status=closing + short_closed_at -> resumes long chase
- V3 spread_close_order_id -> resumes spread-close poll
- V3 closed manual close + long_close_price=null -> fallback is 0.0 only for manual/admin mechanisms
- V3 stop-filled event cannot trigger spread-close order
```

### 6.3 Compatibility exit simulation

The fake broker should prove V4 emits the same sequence as V3:

Manual kill:

```text
claim manual close
persist close_only_mode
cancel stop
if cancel result filled -> stop-filled path
else place spread-close debit order
poll fill
record leg fills / fallback
close position
```

Exchange stop filled:

```text
record short close
wait configured delay
sell long hedge / chase long
close position
```

Software breach:

```text
detect spread mark >= breach threshold
cancel stop
if stop filled -> stop-filled path
else buy-to-close short limit
wait delay
sell long hedge
close position
```

---

## 7. Strategy configuration design

Current `strategies.yaml` should evolve from class-only configuration to contract-driven configuration.

Example:

```yaml
strategies:
  - id: MEIC_IC
    enabled: true
    asset_class: index_option
    instrument: SPX
    structure: iron_condor_credit_verticals
    schedule:
      mode: fixed_slots
      timezone: America/Chicago
    entry_policy: meic_credit_spread_entry
    exit_policy: meic_credit_spread_exit_v3
    risk_policy: meic_daily_risk
    broker: tastytrade
    paper_only: false

  - id: SPX_CALL_DEBIT_TREND
    enabled: false
    asset_class: index_option
    instrument: SPX
    structure: debit_vertical
    signal_gate: spx_trend_vix_filter
    entry_policy: call_debit_vertical_entry
    exit_policy: debit_spread_loss_or_time_exit
    risk_policy: directional_options_risk
    broker: tastytrade
    paper_only: true

  - id: MES_TREND_FOLLOW
    enabled: false
    asset_class: future
    instrument: MES
    structure: future_directional
    signal_gate: mes_trend_filter
    entry_policy: futures_market_or_limit_entry
    exit_policy: futures_bracket_exit
    risk_policy: futures_intraday_risk
    broker: tastytrade
    paper_only: true
```

Validation must be strategy/structure-specific, not globally credit-spread-specific.

---

## 8. Entry architecture

### 8.1 Current issue

Current entry logic is primarily credit-spread oriented. A debit spread or futures strategy should not be forced through `CreditEntryConfig` or credit scanner logic.

### 8.2 Target entry flow

```text
StrategyGate receives SignalSnapshot
  -> strategy emits TradeIntent
  -> EntryPolicy validates constraints
  -> InstrumentService resolves expiry/strikes/contracts
  -> ExecutionEngine builds OrderIntent
  -> BrokerAdapter places order
  -> PositionStore writes PositionState
```

### 8.3 Initial EntryPolicy types

```text
MeicCreditVerticalEntryPolicy
ManualCreditSpreadEntryPolicy
DebitVerticalEntryPolicy
SingleOptionEntryPolicy
MultiLegOptionEntryPolicy
FuturesDirectionalEntryPolicy later
```

### 8.4 Do not add debit spreads until MEIC adapter passes

The first V4 milestone is not debit spreads. It is:

```text
current MEIC credit spread behavior reproduced through generic contracts using fake broker
```

---

## 9. Exit architecture

### 9.1 Use V3 concepts as the base

V4 should use the V3 exit design as the starting point, but make it position-generic.

```text
ExitSupervisor
  -> scans PositionState files / store rows
  -> checks command files or command table
  -> checks market snapshots
  -> dispatches ExitJob
  -> ExitWorkerPool with real priority queue
  -> BrokerLane / BrokerAdapter
```

### 9.2 Real priority queue from the start

V3 currently documents that manual-kill jobs start immediately and compete on a semaphore. V4 should implement a real queue:

```text
Priority 0: emergency operator kill / global flatten
Priority 1: stop filled / broker-confirmed risk event
Priority 2: manual close
Priority 3: software breach
Priority 4: phase upgrade / maintenance
Priority 5: routine reconcile
```

### 9.3 Resume rules

On restart:

```text
closed/cancelled -> no-op
exit_state.close_only_mode=true -> resume exit_handler
working_order_ids present -> reconcile broker first
short/primary risk leg already closed -> resume hedge/long/futures cleanup
exit_stalled=true -> operator alert + broker reconcile before replacement job
```

### 9.4 Position-specific handlers

Initial V4 handlers:

```text
CreditVerticalExitHandler
ManualCloseExitHandler
StopFilledHedgeCleanupHandler
SoftwareBreachExitHandler
```

Later:

```text
DebitVerticalExitHandler
SingleOptionExitHandler
IronCondorExitHandler
FuturesBracketExitHandler
PortfolioFlattenHandler
```

---

## 10. Market data and signal architecture

The existing market-data collector is useful. V4 should keep the concept but expose typed snapshots.

### 10.1 Data layers

```text
MQTT latest quote cache
  -> BarBuilder / OHLC store
  -> IndicatorEngine
  -> SignalEngine
  -> SignalSnapshot
  -> StrategyGate
```

### 10.2 SignalSnapshot

```python
@dataclass
class SignalSnapshot:
    snapshot_id: str
    timestamp: str
    symbols: dict[str, dict]
    signals: dict[str, dict]
    regime: dict[str, str]
    quality: dict[str, bool]
```

Example:

```json
{
  "timestamp": "2026-07-05T14:35:00-05:00",
  "symbols": {
    "SPX": {"close": 6250.50, "ema_21": 6244.20, "trend": "up"},
    "VIX": {"close": 14.8, "ema_21": 15.2, "trend": "down"}
  },
  "signals": {
    "risk_on": {"value": true, "strength": 0.7},
    "spx_trend": {"direction": "bullish", "strength": 0.6},
    "vol_filter": {"state": "safe"}
  }
}
```

### 10.3 SignalGate examples

MEIC:

```text
allow entry only if volatility regime is acceptable and no risk-off signal is active
```

Debit spread:

```text
open bullish call debit spread only if SPX trend is bullish and VIX filter is not risk-off
```

Futures:

```text
open MES long only if trend signal, session filter, and portfolio risk are aligned
```

---

## 11. Futures architecture

Futures should not be forced into option-leg fields.

### 11.1 FuturesPosition

A futures position is still a `PositionState`, but with one or more futures legs and no option premium type.

```text
asset_class = future
structure = future_directional
premium_type = none
legs = [future contract leg]
risk = tick_size, tick_value, stop_points, target_points, max_daily_loss
```

### 11.2 Futures exit policies

```text
FuturesBracketExitPolicy
FuturesTrailingStopPolicy
FuturesTimeExitPolicy
FuturesEmergencyFlattenPolicy
```

### 11.3 Futures should start paper/fake only

Do not connect futures to live execution until:

```text
- generic PositionState works for MEIC
- fake futures broker tests pass
- session hours / roll logic are implemented
- tick value PnL is tested
- bracket/OCO behavior is broker-verified
```

---

## 12. Risk and portfolio control

V4 needs risk above individual strategies.

### 12.1 PortfolioRiskManager

```python
class PortfolioRiskManager:
    def approve_entry(self, intent: TradeIntent, portfolio: PortfolioState) -> RiskDecision: ...
    def approve_exit(self, job: ExitJob, portfolio: PortfolioState) -> RiskDecision: ...
    def update_after_fill(self, position: PositionState, fill: FillEvent) -> None: ...
```

### 12.2 Required risk dimensions

```text
- max daily loss
- max daily profit lock
- max open positions
- max positions per strategy
- max positions per underlying
- max correlated exposure
- max broker in-flight orders
- 0DTE cutoff rules
- emergency flatten rules
```

### 12.3 V3 lesson

Exits should usually bypass entry-style risk rejection. Risk control may choose how to exit, but it should not block a required safety close.

---

## 13. State store design

V4 can start with files but should make the storage interface swappable.

```python
class PositionStore(Protocol):
    def list_active(self) -> list[PositionState]: ...
    def load(self, position_id: str) -> PositionState: ...
    def save_atomic(self, position: PositionState) -> None: ...
    def append_event(self, position_id: str, event: dict) -> None: ...
```

Initial backend:

```text
JSON files + atomic write + event JSONL log
```

Later backend:

```text
SQLite or PostgreSQL/TimescaleDB
```

V4 should keep event history from the start:

```text
position.created
order.submitted
order.accepted
order.filled
exit.accepted
exit.step
exit.stalled
position.closed
```

---

## 14. V4 implementation roadmap

### V4-0 — Separate repo skeleton

Deliverables:

```text
- new repo/branch
- no live broker credentials
- package layout
- fake broker
- test harness
- core contracts module
- sample V3 trade JSON fixtures
```

Pass criteria:

```text
pytest passes
fake broker tests deterministic
no production order placement code active
```

### V4-1 — MEIC compatibility adapter

Deliverables:

```text
- V3 MEIC JSON -> PositionState
- PositionState -> V3-like action simulation
- manual kill simulation
- stop-filled long-chase simulation
- software breach simulation
- V2.9 long_close_price fallback test
```

Pass criteria:

```text
same intended broker action sequence as V3 for core exit paths
```

### V4-2 — Generic option structures

Deliverables:

```text
- credit vertical model
- debit vertical model
- single option model
- multi-leg option model skeleton
- instrument-aware tick/symbol handling
```

Pass criteria:

```text
debit spread does not use credit-spread breach math
SPY/QQQ tick rules do not use SPX tick rules
```

### V4-3 — Signal engine and strategy gates

Deliverables:

```text
- SignalSnapshot
- SignalEngine reading OHLC/indicator files
- StrategyGate
- signal-to-TradeIntent tests
```

Pass criteria:

```text
signals can allow/deny MEIC entry without touching exit engine
```

### V4-4 — Broker adapter layer

Deliverables:

```text
- BrokerAdapter protocol
- FakeBrokerAdapter complete
- TastyTradeAdapter skeleton
- rate limiter / broker lane abstraction
- no live order placement by default
```

Pass criteria:

```text
all integration tests use fake broker unless explicitly marked paper/live
```

### V4-5 — Futures paper prototype

Deliverables:

```text
- FuturesPosition
- tick value PnL
- session definitions
- roll metadata placeholder
- fake futures bracket tests
```

Pass criteria:

```text
no futures code depends on option short_leg/long_leg schema
```

---

## 15. Testing matrix

| Test area | Required examples |
|---|---|
| V3 compatibility | Current trade JSON maps into PositionState |
| Manual kill | open + close_only_mode resumes manual exit after restart |
| Stop filled | filled stop never places spread close |
| Breach | breach cancel filled routes to stop-filled path |
| Command claiming | duplicate commands cannot create duplicate exit jobs |
| Atomic writes | interrupted write cannot corrupt state file |
| Tick rules | SPX, SPY, QQQ, MES rounding differ correctly |
| Debit spread | loss/profit math is not inverted accidentally |
| Futures | tick value and stop points tested |
| Signal gate | signal can emit TradeIntent without broker calls |
| Broker adapter | fake broker records exact order sequence |
| Risk manager | rejects new entries but permits safety exits |
| Restart recovery | every persisted exit handler resumes or alerts |

---

## 16. Codebase layout proposal

```text
v4_trading_platform/
  contracts/
    instruments.py
    symbols.py
    ticks.py
    orders.py
    intents.py
    positions.py
    exits.py
    signals.py
  adapters/
    v3_meic_json.py
  brokers/
    base.py
    fake.py
    tastytrade.py
  market_data/
    snapshots.py
    bars.py
    indicators.py
    signals.py
  strategies/
    base.py
    gates.py
    meic_compat.py
  execution/
    entry_engine.py
    order_builder.py
  exits/
    supervisor.py
    worker_pool.py
    policies.py
    handlers/
      manual_close.py
      credit_vertical.py
      stop_filled.py
      debit_vertical.py
      futures.py
  risk/
    portfolio.py
    daily_limits.py
  storage/
    position_store.py
    json_store.py
    event_log.py
  tests/
    fixtures/
      v3_meic_trades/
```

---

## 17. What not to do

Do not:

```text
- refactor live V3 into V4 while trades are being monitored
- add debit spreads before MEIC compatibility is proven
- force futures into option-leg schema
- let strategies place broker orders directly
- use SPX tick rounding globally
- build a second stop-monitor process model for futures unless fake tests prove it is needed
- skip fake broker tests and go straight to paper/live
- ignore V3 incidents; every V3 exit lesson should become a V4 regression test
```

---

## 18. Coding-agent prompt

Use this prompt to start V4 work in a separate repo/prototype.

```text
You are building V4 of the MEIC trading architecture as a separate repo/prototype. Do not modify live V3 production behavior unless explicitly asked.

Context:
- V3 is the live stop-monitor safety system for MEIC/manual SPX/SPXW credit spreads.
- V3 already implemented StopSupervisor, TradeSlot, ExitWorkerPool, command claiming, atomic writes, close_only_mode, handler-based manual kill, and V2 rollback.
- V4 must learn from V3 but must not destabilize V3.

Goal:
Build a generic multi-strategy architecture that can eventually support MEIC credit spreads, debit spreads, multi-leg options, signal-driven entries, and futures.

Hard rules:
1. No live broker order placement in V4 initially.
2. Use FakeBrokerAdapter and deterministic tests first.
3. MEIC compatibility comes before debit spreads or futures.
4. Strategies emit TradeIntent only; they do not place orders.
5. Entry handlers convert TradeIntent into OrderIntent.
6. Exit handlers consume PositionState and ExitPolicy; they do not depend on MEIC tranche names.
7. One active exit owner per position.
8. Persisted exit ownership must resume after restart.
9. Broker I/O must be bounded, observable, and outside supervisor scan loops.
10. Any live V3 bug discovered later must be fixed in V3 first, then added to V4 as a regression test or architecture rule.

First milestone, V4-0:
- Create repo/package skeleton.
- Add core contracts:
  - InstrumentSpec
  - TickRules
  - SymbolCodec / InstrumentSymbol
  - OrderLeg
  - OrderIntent
  - TradeIntent
  - PositionLeg
  - PositionState
  - ExitState
  - ExitPolicy
  - ExitHandler
  - BrokerAdapter
  - SignalSnapshot
- Add FakeBrokerAdapter.
- Add JSON PositionStore with atomic writes and event log.
- Add pytest suite.
- No live broker adapter beyond a non-functional skeleton.

Second milestone, V4-1:
- Build V3 MEIC compatibility adapter.
- Read current V3 MEIC trade JSON.
- Convert short_leg/long_leg into PositionLeg[].
- Convert active_stop, spread_close_order_id, close_only_mode, exit_handler into PositionState/ExitState.
- Simulate V3 manual kill, stop-filled long chase, and software breach using FakeBrokerAdapter.
- Add tests:
  - open + close_only_mode resumes ManualCloseExitPolicy after restart
  - stop-filled event never places spread-close order
  - manual/admin close with missing long_close_price uses 0.0 fallback
  - duplicate command cannot create duplicate exit jobs
  - working order IDs reconcile before replacement worker

Third milestone, V4-2:
- Add generic option structures:
  - credit vertical
  - debit vertical
  - single option
  - multi-leg option skeleton
- Add instrument-aware tick and symbol rules for SPX/SPXW, SPY, QQQ.
- Prove debit spread stop/profit logic does not reuse credit-spread math.

Fourth milestone, V4-3:
- Add SignalSnapshot and SignalEngine.
- Read existing OHLC/indicator files.
- Implement simple gates: VIX filter, SPX EMA trend, risk-on/risk-off.
- StrategyGate converts signal context into TradeIntent.

Fifth milestone, V4-4:
- Add broker adapter layer and bounded BrokerLane abstraction.
- Keep live order placement disabled by default.
- Add paper/live tests only behind explicit flags.

Sixth milestone, V4-5:
- Add futures prototype with fake broker only:
  - FuturesPosition
  - tick size / tick value PnL
  - bracket/OCO semantics
  - session definitions
  - no option-leg schema dependency

Deliverables for each milestone:
- Code
- Tests
- README section explaining how it maps to V3 lessons
- No production behavior changes in V3 unless explicitly requested
```

---

## 19. Final verdict

V4 should start now as a separate prototype. The correct relationship is:

```text
V3 watches live trades.
V4 learns from V3 and becomes the future platform.
```

Do not remove V3 content from the multi-strategy architecture review. Instead, mark V3 as the implemented exit baseline and build V4 around generic contracts that preserve those safety lessons.

