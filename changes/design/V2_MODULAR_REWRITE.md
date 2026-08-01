# V2 Architecture — Modular Rewrite Analysis

**Date**: Jun 21, 2026
**Status**: Design / Analysis — no code changes
**Scope**: Separate repo, clean-room rewrite of the trading engine as composable building blocks

---

## Executive Summary

The current MEIC codebase (V1) works, but the strategy logic (entry timing, credit targets, stop math, phase transitions) is welded directly into the stop_monitor, entry pipeline, and config. Adding a second strategy (e.g., Iron Fly, Debit Spreads, Ratio Spreads) would require forking the entire stop_monitor and entry flow — defeating the purpose of a multi-strategy platform.

V2 decomposes the system into **four primitive building blocks** that strategies assemble like LEGO:

| Block | Responsibility | Reusable across |
|-------|---------------|-----------------|
| **Stop** | Monitor a position, manage exchange stops, detect breaches, close legs | Any credit or debit spread |
| **Streamer** | Subscribe to symbols, publish live prices, detect staleness | Everything |
| **Credit Spread Entry** | Scan strikes, evaluate credit targets, place NET_CREDIT order | Any credit spread strategy |
| **Debit Spread Entry** | Scan strikes, evaluate debit targets, place NET_DEBIT order | Any debit spread strategy (future) |

A **Strategy** is a YAML/Python definition that wires these blocks together with its own conditions:

```
MEIC Strategy:
  Entry:  CreditSpreadEntry(time_slots=[11:00, 12:00, ...], credit_target=0.90–1.85)
  Stop:   Stop(initial=2x_short, phase2=2x_credit_when_long≤0.05, phase3=proximity_close)
```

---

## Part 1: What V1 Gets Right (Preserve These)

Before tearing anything apart, here is what works and must be carried forward:

### 1.1 Proven Trading Logic

| V1 Feature | How it works | Keep in V2? |
|------------|-------------|-------------|
| 4-step stop lifecycle | Phase 1 (2x short stop) → Phase 2 (2x net credit when long ≤ $0.05) → Phase 3 (SPX proximity close at 2:51 PM) → 3 PM admin close | Yes — as a **stop profile** the MEIC strategy references |
| Dual safety net | Exchange STOP_LIMIT at broker + software breach via MQTT prices | Yes — both mechanisms live inside the Stop block |
| Spread-mid breach formula | `spread_price = short_mid - long_mid >= threshold + $0.20` | Yes — configurable in Stop block |
| Long close chase loop | Cancel-and-reprice at MQTT mid every 3s, escalate to market after 10 attempts | Yes — built into Stop block |
| 30s intentional delay before long close | Let long leg appreciate in trending market before selling | Yes — configurable `LONG_CLOSE_DELAY_SEC` |
| Fire-and-forget entry | Thin tranche places order, writes handshake JSON, exits. Stop monitor takes over | Yes — clean separation of concerns |
| AlertListener for instant fill detection | Sub-second stop fill notification via TastyTrade websocket | Yes — Stop block integrates AlertListener |
| Atomic JSON state with Windows retry | `tempfile` + `os.replace` with 8-attempt retry for Windows locks | Yes — State module carries forward |

### 1.2 Architectural Patterns to Keep

- **File-based IPC** between entry and stop (handshake JSON in `trades/active/`)
- **MQTT as a price bus** with kill switch and dynamic symbol subscription
- **Per-trade JSON** as the single source of truth per spread side
- **Dashboard stays broker-free** — reads files and MQTT, writes command files
- **Session refresh** (every 20 min) with auto-retry on 401

### 1.3 What V1 Gets Wrong (Motivations for V2)

| Problem | Where it lives in V1 | Why it hurts |
|---------|----------------------|--------------|
| **MEIC entry logic is hardcoded** | `open_spread_tt.py` has credit_min/max, OTM range, width range baked in from `config.py` | Can't reuse for Iron Fly or different credit targets without forking |
| **Stop phases are MEIC-specific** | `phases.py` has Phase1/Phase2/Phase3 hardcoded with MEIC thresholds | Another strategy with different stop logic (e.g., trailing stop, time-based exit) requires new phase classes |
| **Config is monolithic** | `meic0dte/app/config.py` has 20+ constants for one strategy | No namespace isolation — adding a second strategy pollutes the same file |
| **Strategy = "everything"** | No formal Strategy object. The strategy IS the config + entry + stop_monitor | Can't compose, can't disable one strategy while running another |
| **`MEIC_IC` string peppered everywhere** | `state_mod.state_filename('MEIC_IC', ...)`, strategy names in JSON | Adding `IRON_FLY` requires touching 10+ places |
| **Breach logic assumes credit spreads** | `spread_mark_price = short - long` and `spread >= threshold` | Debit spreads have inverted P&L semantics; the formulas break |
| **No strategy-level enable/disable** | Tranche schedule is global in `run.py` | Can't run MEIC on slots 1-4 and Iron Fly on slots 5-6 |

---

## Part 2: The Four Building Blocks

### 2.1 Stop Block

The Stop block is the most critical piece. It manages the entire post-entry lifecycle of a spread position.

#### Interface

```python
class StopBlock:
    """Manages exchange stop, breach detection, phase transitions, and close lifecycle."""

    def __init__(
        self,
        trade_state: TradeState,
        broker: BrokerBase,
        price_feed: PriceFeed,
        stop_profile: StopProfile,     # ← strategy provides this
        alert_listener: AlertListener,
    ): ...

    def run(self) -> None:
        """Main loop: fast breach check (3s), slow broker sync (60s), long chase."""

    def handle_fill_event(self, event: FillEvent) -> None:
        """AlertListener callback when exchange stop fills."""

    def force_close(self, mechanism: str) -> None:
        """Dashboard kill switch / manual close."""
```

#### StopProfile — The Strategy's Configuration for the Stop Block

This is what makes the Stop block reusable. Instead of hardcoding Phase 1/2/3, the strategy provides a **StopProfile**:

```python
@dataclass
class StopProfile:
    # --- Initial stop ---
    initial_stop_calc: Callable[[TradeState], StopPrice]
    # e.g., for MEIC: lambda s: (s.short_fill - 0.10) * 2.0

    # --- Phase transitions (ordered list) ---
    phases: list[PhaseRule]

    # --- Breach detection ---
    breach_calc: Callable[[float, float], float]
    # e.g., spread_mark_price = short_mid - long_mid (credit spread)
    # e.g., long_mid - short_mid (debit spread)

    breach_condition: Callable[[float, float], bool]
    # e.g., spread_price >= threshold + 0.20 (credit)
    # e.g., spread_price <= threshold - 0.20 (debit)

    # --- Long close behavior ---
    long_close_delay_sec: int = 30
    long_chase_max_attempts: int = 10
    long_chase_escalate_to_market: bool = True

    # --- End-of-day ---
    proximity_check_time: time = time(14, 51)
    proximity_threshold: float = 3.0
    hard_close_time: time = time(15, 0)
```

#### PhaseRule — Composable Phase Transitions

Instead of hardcoded Phase1/Phase2/Phase3 classes, phases become data:

```python
@dataclass
class PhaseRule:
    name: str
    priority: int

    # When does this phase activate?
    condition: Callable[[TradeState, PriceFeed], bool]

    # What does it do?
    action: Callable[[StopBlock], None]

    # Can it fire more than once?
    one_shot: bool = True
```

**MEIC's phases expressed as PhaseRules:**

```python
meic_phases = [
    PhaseRule(
        name='phase1_initial_stop',
        priority=10,
        condition=lambda state, prices: state.status == 'open',
        action=lambda stop: stop.check_breach_and_manage_stop(),
        one_shot=False,  # runs every cycle
    ),
    PhaseRule(
        name='phase2_net_credit_upgrade',
        priority=20,
        condition=lambda state, prices: (
            state.status == 'open'
            and not state.phases.short_stoplmt_replaced
            and prices.get(state.long_leg.symbol) <= 0.05
        ),
        action=lambda stop: stop.upgrade_to_spread_stop(),
        one_shot=True,
    ),
    PhaseRule(
        name='phase3_spx_proximity',
        priority=30,
        condition=lambda state, prices: (
            state.status == 'open'
            and central_time() >= time(14, 51)
        ),
        action=lambda stop: stop.execute_proximity_close(),
        one_shot=False,
    ),
]
```

**Why this matters**: A trailing stop strategy would define completely different PhaseRules:

```python
trailing_phases = [
    PhaseRule(
        name='trailing_stop_adjust',
        priority=10,
        condition=lambda state, prices: state.status == 'open' and prices.spread_pnl(state) > 0,
        action=lambda stop: stop.tighten_stop_to_breakeven(),
        one_shot=False,
    ),
]
```

No changes to the Stop block itself — only the profile changes.

#### What the Stop Block Owns vs What It Delegates

| Responsibility | Stop block owns it | Strategy provides it |
|---------------|-------------------|---------------------|
| Exchange stop placement (STOP_LIMIT order) | Yes | Initial price via `initial_stop_calc` |
| Breach detection loop (every 3s, MQTT only) | Yes | Breach formula via `breach_calc` + `breach_condition` |
| Cancel-and-replace to limit on breach | Yes | — |
| Long close chase loop | Yes | Delay, max attempts, escalation policy |
| Phase transitions | Yes (execution engine) | Phase rules (conditions + actions) |
| AlertListener re-registration on order ID change | Yes | — |
| State persistence (JSON atomic write) | Yes | — |
| Broker retry with exponential backoff | Yes | — |
| Dashboard command file detection | Yes | — |
| Stop history audit trail | Yes | — |

#### State Schema (V2)

The trade state JSON becomes strategy-agnostic:

```json
{
  "strategy": "MEIC_IC",
  "strategy_version": "1.0",
  "instrument": "SPX",
  "spread_type": "credit",

  "status": "open",
  "lot": "11-00",
  "side": "P",

  "entry": {
    "timestamp": "...",
    "net_credit": 1.45,
    "limit_price": 1.45,
    "order_id": "477..."
  },
  "short_leg": {
    "symbol": ".SPXW260621P7410",
    "occ_symbol": "SPXW  260621P07410000",
    "strike": 7410,
    "fill_price": 1.75,
    "action": "SELL_TO_OPEN"
  },
  "long_leg": {
    "symbol": ".SPXW260621P7385",
    "occ_symbol": "SPXW  260621P07385000",
    "strike": 7385,
    "fill_price": 0.30,
    "action": "BUY_TO_OPEN"
  },

  "stop_profile": "meic_2x_short",
  "active_stop": { ... },
  "stop_history": [ ... ],
  "phases_state": {
    "phase2_activated": false,
    "phase3_activated": false
  },

  "close": null,
  "close_mechanism": null,
  "recovery": { ... }
}
```

Key changes from V1:
- `spread_type` field ("credit" / "debit") tells the Stop block which breach formula to use
- `stop_profile` references a named profile instead of hardcoding phase logic
- `strategy` and `strategy_version` for audit and routing
- `instrument` for multi-ticker support
- Leg-level `action` field (SELL_TO_OPEN vs BUY_TO_OPEN) — critical for debit spreads where the roles flip

---

### 2.2 Streamer Block

The Streamer is already close to being a standalone block in V1. V2 formalizes it.

#### Interface

```python
class StreamerBlock:
    """Manages DXLink → MQTT price feed with dynamic symbol subscription."""

    def __init__(
        self,
        session: TastyTradeSession,
        mqtt_config: MqttConfig,
        topic_prefix: str = 'TASTYTRADE/',
    ): ...

    def start(self) -> None:
        """Connect DXLink, subscribe SPX, start MQTT publish loop."""

    def add_symbols(self, symbols: list[str]) -> None:
        """Subscribe to additional option symbols (called by Entry block after fill)."""

    def remove_symbols(self, symbols: list[str]) -> None:
        """Unsubscribe symbols no longer needed (after close)."""

    def stop(self) -> None:
        """Shutdown at market close."""
```

#### What changes from V1

| Aspect | V1 | V2 |
|--------|----|----|
| Symbol file | `optsymbols.json` (append-only, dedup on write) | Same file, but Streamer also supports `remove_symbols()` for cleanup after close |
| Kill switch | Reads MQTT kill topic | Same — kill switch is a cross-cutting concern, stays on MQTT |
| 3 PM shutdown | Hardcoded `central_time() >= 15:00` | Configurable `market_close_time` (for futures, extended hours) |
| Index symbol | Hardcoded `SPX` | Configurable via `instrument_config.index_symbol` |
| Reconnect | DXLink websocket reconnect with backoff | Same, with health heartbeat JSON |
| Staleness detection | None (identified in GAP-09) | Built-in: publishes `heartbeat.json` with `last_price_ts`. Stop block and dashboard read it. If no price update in 30s during market hours → CRITICAL alert |

#### Staleness Guard (New in V2)

The streamer publishes a heartbeat every 5 seconds:

```json
{
  "ts": "2026-06-21T12:30:05-05:00",
  "last_spx_price_ts": "2026-06-21T12:30:04-05:00",
  "symbols_subscribed": 26,
  "msgs_per_min": 142,
  "status": "live"
}
```

The Stop block reads this. If `last_spx_price_ts` is older than 30 seconds during market hours, the Stop block **freezes all breach decisions** and logs CRITICAL. It does NOT close positions on stale data — that was identified as the most dangerous failure mode in GAP-09 analysis.

---

### 2.3 Credit Spread Entry Block

Handles the entire open flow for credit spreads: scan strikes → evaluate credit → place order → write handshake JSON → register symbols with streamer.

#### Interface

```python
class CreditSpreadEntry:
    """Scan and open a credit spread (PCS or CCS)."""

    def __init__(
        self,
        broker: BrokerBase,
        price_feed: PriceFeed,
        entry_config: CreditEntryConfig,
    ): ...

    def execute(self, lot: str, side: str) -> TradeState:
        """
        Full entry flow:
        1. Get SPX price from MQTT
        2. Scan candidate strikes
        3. Evaluate credit for each pair
        4. Place NET_CREDIT spread order
        5. Write handshake JSON
        6. Register symbols with streamer
        7. Wait briefly for fill (FILL_WAIT_MAX seconds)
        8. Return state for stop_monitor to pick up
        """
```

#### CreditEntryConfig — Strategy Controls Entry Behavior

```python
@dataclass
class CreditEntryConfig:
    # Strike selection
    spread_width_range: tuple[int, int] = (25, 35)     # min, max
    spread_width_step: int = 5
    otm_range: tuple[int, int] = (5, 150)              # min, max from ATM
    otm_step: int = 5
    strike_step: int = 5                                # SPX = 5-wide

    # Credit targets
    credit_min: float = 0.90
    credit_max_put: float = 1.85
    credit_max_call: float = 1.85

    # Order behavior
    quantity: int = 1
    fill_wait_max: int = 5                              # seconds before handing off
    max_open_attempts: int = 10

    # Overlap guard
    check_strike_conflicts: bool = True                 # prevent same long strike as another lot's short

    # Price adjustment
    open_price_adj: float = 0.05                        # nickel rounding bias
```

**Why a separate config**: MEIC uses `credit_min=0.90, width=25-35`. Another credit spread strategy might use `credit_min=2.00, width=50-75` for wider, higher-premium trades. Same Entry block, different config.

#### Entry Flow (Detailed)

```
CreditSpreadEntry.execute("11-00", "P")
  │
  ├── 1. spx = price_feed.get_spx()
  │     └── Reads from MQTT cache (no API call)
  │
  ├── 2. spx_rounded = round(spx / 5) * 5
  │
  ├── 3. candidates = scan_strikes(spx_rounded, "P", config)
  │     └── Generates all (short_strike, long_strike) pairs
  │         within width_range × otm_range
  │
  ├── 4. register_candidate_symbols(candidates)
  │     └── Write to optsymbols.json → streamer subscribes
  │     └── Wait STREAMER_QUOTE_WAIT seconds for MQTT to flow
  │
  ├── 5. for (short, long) in candidates:
  │     ├── short_mid = price_feed.get(short.symbol)
  │     ├── long_mid = price_feed.get(long.symbol)
  │     ├── credit = short_mid - long_mid
  │     ├── if credit < credit_min: break (further OTM won't help)
  │     ├── if credit_min <= credit <= credit_max:
  │     │     ├── check_strike_conflicts(short, long)
  │     │     └── FOUND → break
  │     └── continue
  │
  ├── 6. broker.place_spread_order(short, long, qty, credit)
  │     └── Returns order_id
  │
  ├── 7. write_pending_trade_state(...)
  │     └── Handshake JSON in trades/active/
  │
  ├── 8. register_spread_symbols(state)
  │     └── Ensure streamer has the filled strikes
  │
  └── 9. wait_and_sync_fill(broker, order_id, ...)
        └── Poll up to FILL_WAIT_MAX, update JSON
        └── Return state → stop_monitor takes over
```

---

### 2.4 Debit Spread Entry Block (Future)

For strategies that BUY spreads (e.g., long verticals, butterflies):

```python
class DebitSpreadEntry:
    """Scan and open a debit spread."""

    def __init__(
        self,
        broker: BrokerBase,
        price_feed: PriceFeed,
        entry_config: DebitEntryConfig,
    ): ...

    def execute(self, lot: str, side: str) -> TradeState:
        """Same flow as CreditSpreadEntry but:
        - Evaluates debit = long_mid - short_mid (BUY the expensive leg)
        - Places NET_DEBIT order (negative price in TastyTrade)
        - Leg actions flip: BUY_TO_OPEN on the "short" (really long) leg
        """
```

#### Key Differences from Credit Entry

| Aspect | Credit Spread Entry | Debit Spread Entry |
|--------|--------------------|--------------------|
| P&L direction | Collect premium upfront, risk defined | Pay premium upfront, profit from move |
| Price calc | `credit = short_mid - long_mid` | `debit = long_mid - short_mid` |
| Order type | NET_CREDIT (positive price) | NET_DEBIT (negative price in TastyTrade) |
| Leg actions | SELL_TO_OPEN short, BUY_TO_OPEN long | BUY_TO_OPEN "main" leg, SELL_TO_OPEN "hedge" leg |
| Stop semantics | Stop when spread widens (losing money) | Stop when spread narrows (losing money) |
| Breach formula | `spread_price >= threshold` | `spread_price <= threshold` |

The Stop block handles both — the `StopProfile.breach_calc` and `breach_condition` abstract the direction.

---

## Part 3: The Strategy Layer

A Strategy is the glue that assembles blocks with its own conditions.

### 3.1 Strategy Definition

```python
class StrategyBase(ABC):
    """Every strategy must define its building blocks and conditions."""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def instrument(self) -> str: ...

    @abstractmethod
    def entry_block(self) -> EntryBlock: ...

    @abstractmethod
    def stop_profile(self) -> StopProfile: ...

    @abstractmethod
    def schedule(self) -> list[TrancheSlot]: ...

    def pre_entry_check(self, prices: PriceFeed) -> bool:
        """Optional gate: VIX check, time-of-day filter, max exposure, etc."""
        return True
```

### 3.2 MEIC Strategy Implementation

```python
class MEICStrategy(StrategyBase):
    name = "MEIC_IC"
    instrument = "SPX"

    def entry_block(self):
        return CreditSpreadEntry(
            broker=self.broker,
            price_feed=self.prices,
            entry_config=CreditEntryConfig(
                spread_width_range=(25, 35),
                credit_min=0.90,
                credit_max_put=1.85,
                credit_max_call=1.85,
                quantity=1,
            ),
        )

    def stop_profile(self):
        return StopProfile(
            initial_stop_calc=meic_initial_stop,     # (short_fill - 0.10) * 2.0
            phases=meic_phases,                       # Phase1/2/3 as PhaseRules
            breach_calc=spread_mark_price,            # short - long
            breach_condition=lambda spread, thresh: spread >= thresh,
            long_close_delay_sec=30,
            proximity_check_time=time(14, 51),
            proximity_threshold=3.0,
        )

    def schedule(self):
        return [
            TrancheSlot("11-00", time(10, 59), time(11, 5), sides=["P", "C"]),
            TrancheSlot("12-00", time(11, 59), time(12, 5), sides=["P", "C"]),
            TrancheSlot("12-30", time(12, 29), time(12, 35), sides=["P", "C"]),
            TrancheSlot("01-15", time(13, 14), time(13, 20), sides=["P", "C"]),
            TrancheSlot("01-45", time(13, 44), time(13, 50), sides=["P", "C"]),
            TrancheSlot("02-00", time(13, 59), time(14, 5), sides=["P", "C"]),
        ]

    def pre_entry_check(self, prices):
        # Could add VIX gating here
        return True
```

### 3.3 Example: A Different Strategy Using the Same Blocks

```python
class WideWingStrategy(StrategyBase):
    """High-premium credit spreads with tighter stops and no Phase 2."""
    name = "WIDE_WING"
    instrument = "SPX"

    def entry_block(self):
        return CreditSpreadEntry(
            broker=self.broker,
            price_feed=self.prices,
            entry_config=CreditEntryConfig(
                spread_width_range=(50, 75),
                credit_min=2.50,
                credit_max_put=4.00,
                credit_max_call=4.00,
                quantity=2,
                otm_range=(10, 100),
            ),
        )

    def stop_profile(self):
        return StopProfile(
            initial_stop_calc=lambda s: s.short_leg.fill_price * 1.5,  # 1.5x, not 2x
            phases=[
                PhaseRule(
                    name='monitor_only',
                    priority=10,
                    condition=lambda s, p: s.status == 'open',
                    action=lambda stop: stop.check_breach_and_manage_stop(),
                    one_shot=False,
                ),
                # No Phase 2 (net credit upgrade)
                # Phase 3 only
                PhaseRule(
                    name='eod_proximity',
                    priority=30,
                    condition=lambda s, p: central_time() >= time(14, 55),
                    action=lambda stop: stop.execute_proximity_close(),
                    one_shot=False,
                ),
            ],
            breach_condition=lambda spread, thresh: spread >= thresh,
            long_close_delay_sec=15,  # Shorter delay
            proximity_threshold=5.0,  # Wider proximity
        )

    def schedule(self):
        return [
            TrancheSlot("11-30", time(11, 29), time(11, 35), sides=["P", "C"]),
            TrancheSlot("01-00", time(12, 59), time(13, 5), sides=["P", "C"]),
        ]
```

Same Stop block, same Entry block, same Streamer — completely different trading behavior.

---

## Part 4: The Scheduler / Orchestrator

### 4.1 How Strategies Get Scheduled

The launcher (`run.py` equivalent) becomes a strategy-aware scheduler:

```python
class Orchestrator:
    """Loads strategies, runs their schedules, manages shared infrastructure."""

    def __init__(self):
        self.strategies: list[StrategyBase] = load_enabled_strategies()
        self.streamer = StreamerBlock(...)
        self.broker = get_broker()
        self.stop_monitor = StopMonitorSupervisor(...)

    def run(self):
        self.streamer.start()
        self.stop_monitor.start()

        while market_is_open():
            now = central_time()
            for strategy in self.strategies:
                for slot in strategy.schedule():
                    if slot.is_in_window(now) and not slot.already_fired:
                        if strategy.pre_entry_check(self.prices):
                            self.fire_tranche(strategy, slot)
                            slot.already_fired = True
            sleep(5)
```

### 4.2 Strategy Registry (`strategies.yaml`)

```yaml
strategies:
  - name: MEIC_IC
    enabled: true
    module: strategies.meic.MEICStrategy
    config_overrides:
      credit_min: 0.90
      quantity: 1

  - name: WIDE_WING
    enabled: false
    module: strategies.wide_wing.WideWingStrategy
    config_overrides:
      quantity: 2

  - name: Iron_Fly
    enabled: false
    module: strategies.iron_fly.IronFlyStrategy
```

Enable/disable at the YAML level. No code changes to run different combinations.

---

## Part 5: Making It Foolproof — Edge Cases and Guards

### 5.1 Stop Block Guards

| Guard | What it prevents | How |
|-------|-----------------|-----|
| **Stale price freeze** | Breach decision on old MQTT data | If streamer heartbeat > 30s old, skip all breach checks. Log CRITICAL. Exchange stop at broker still protects |
| **Double-fire prevention** | Same phase executing twice concurrently | `_breach_active` flag (exists in V1). V2 adds per-phase dedup via `phases_state` in JSON |
| **Orphan detection** | Long leg order placed but not tracked | `long_close_order_id` required before `status='closing'`. On load, if `closing` with no `long_close_order_id`, re-place |
| **Cancel-confirm loop** | Cancelling a stop that already filled | `_cancel_stop_and_confirm()` polls until broker confirms cancelled or filled. If filled, route to fill handler (V1 has this) |
| **Session expiry mid-trade** | Auth dies during breach response | `_retry_on_transient()` catches 401, auto-refreshes session, retries (V1 has this) |
| **Quantity mismatch** | Partial fill → stop covers wrong qty | `stop_is_current()` checks `stop_quantity` vs `filled_quantity`. If mismatched, resize (V1 has this) |
| **Race: breach vs exchange stop** | Software breach fires on already-filled exchange stop | Cancel stop → broker returns "already filled" → route to fill handler instead of placing duplicate limit (V1 has this) |
| **Kill switch idempotency** | Dashboard kill switch processed multiple times | `killswitch.json` deleted after first processing. State set to `closing` prevents re-entry |
| **Credit vs debit direction** | Wrong breach formula for spread type | `spread_type` in state JSON → Stop block selects correct `breach_calc`. Validated at init |

### 5.2 Entry Block Guards

| Guard | What it prevents | How |
|-------|-----------------|-----|
| **Negative credit rejection** | Stale MQTT data produces debit for credit spread | `if credit <= 0: skip this pair, log warning` (GAP-20 fix formalized) |
| **Strike overlap** | Two lots sharing the same short/long strike | `check_strike_conflicts()` scans `trades/active/*.json` for overlapping symbols (V1 has this) |
| **Max attempts** | Infinite retry loop on unfillable credit | `max_open_attempts` config (default 10). After exhaustion, log and skip |
| **Paused tranche** | Entry on a dashboard-paused slot | Check `pause_tranches.json` before executing |
| **Price source validation** | Entry using API prices instead of MQTT | V2 formalizes: candidate symbols → optsymbols.json → streamer subscribes → MQTT flows → entry reads MQTT. No API calls for pricing during scan |
| **Fill wait timeout** | Holding entry thread indefinitely | `fill_wait_max` (default 5s). After timeout, handshake JSON written, stop_monitor takes over |

### 5.3 Streamer Block Guards

| Guard | What it prevents | How |
|-------|-----------------|-----|
| **DXLink silent disconnect** | Websocket alive but no data flowing | `last_price_ts` in heartbeat. If stale > 30s during market hours → reconnect |
| **Symbol dedup** | Duplicate symbols in optsymbols.json | `set()` dedup before write (V1 has this via GAP-19/21 fix) |
| **Timezone safety** | Shutdown at wrong time on non-CT server | All time checks use `central_time()` (V1 has this via GAP-08 fix) |
| **Reconnect backoff** | Rapid reconnect hammering TastyTrade | Exponential backoff: 1s, 2s, 4s, 8s, max 60s |
| **Process health** | Streamer crash undetected | Orchestrator checks `proc.poll()` every 5s, restarts if exited (V1 has this via GAP-09 fix) |

### 5.4 Cross-Block Guards

| Guard | What it prevents | How |
|-------|-----------------|-----|
| **Strategy isolation** | One strategy's error taking down another | Each strategy's trades are in their own namespace (`trades/active/MEIC_IC_*`, `trades/active/WIDE_WING_*`). Stop errors for one don't affect the other |
| **Shared session** | Duplicate TastyTrade OAuth sessions | Single `BrokerBase` instance shared across strategies. Session refresh managed centrally by Orchestrator |
| **MQTT topic isolation** | Strategies interfering with each other's MQTT | All strategies share the same MQTT price feed (read-only). Command files are per-trade (keyed by trade filename) — no collision |
| **Daily archive** | Yesterday's trades contaminating today | `archive_daily_trades()` at market close moves all `trades/active/` to `trades/history/YYYY-MM-DD/` (V1 has this) |
| **Config validation** | Strategy config with impossible values | `CreditEntryConfig.__post_init__()` validates: `credit_min > 0`, `credit_max >= credit_min`, `width_range[0] < width_range[1]`, etc. |

---

## Part 6: Folder Structure (V2 Repo)

```
spx-engine-v2/
├── run.py                          Orchestrator: load strategies, schedule, health checks
├── strategies.yaml                 Enable/disable strategies
├── requirements.txt
├── .env / .env.example
│
├── blocks/
│   ├── stop/
│   │   ├── __init__.py
│   │   ├── stop_block.py           StopBlock class (main loop, phase engine)
│   │   ├── stop_profile.py         StopProfile, PhaseRule dataclasses
│   │   ├── breach.py               Breach detection functions (credit + debit)
│   │   ├── long_chase.py           Long close chase/replace loop
│   │   ├── fill_sync.py            Entry fill synchronization
│   │   ├── broker_sync.py          REST backup poll, adopt broker stop
│   │   └── alerts.py               AlertListener integration
│   │
│   ├── streamer/
│   │   ├── __init__.py
│   │   ├── streamer_block.py       DXLink → MQTT publisher
│   │   ├── mqtt_prices.py          MqttPriceCache (read side)
│   │   └── health.py               Heartbeat + staleness detection
│   │
│   ├── entry/
│   │   ├── __init__.py
│   │   ├── credit_spread.py        CreditSpreadEntry
│   │   ├── debit_spread.py         DebitSpreadEntry (future)
│   │   ├── entry_config.py         CreditEntryConfig, DebitEntryConfig
│   │   └── strike_scanner.py       Candidate strike generation
│   │
│   └── common/
│       ├── state.py                TradeState schema, atomic JSON I/O
│       ├── option_ticks.py         SPX tick rounding ($0.05 / $0.10 threshold)
│       ├── symbols.py              TastyTrade ↔ OCC symbol translation
│       └── time_utils.py           central_time(), market calendar, holidays
│
├── brokers/
│   ├── base.py                     BrokerBase ABC + OrderResult
│   ├── tastytrade_broker.py        TastyTrade implementation (with retry + session refresh)
│   └── paper_broker.py             Paper session wrapper
│
├── strategies/
│   ├── base.py                     StrategyBase ABC
│   ├── meic/
│   │   ├── __init__.py
│   │   ├── strategy.py             MEICStrategy class
│   │   ├── stop_profile.py         MEIC-specific PhaseRules + stop calc
│   │   └── config.py               MEIC defaults (credit range, stop multipliers)
│   │
│   └── wide_wing/                  (example second strategy)
│       ├── __init__.py
│       ├── strategy.py
│       └── config.py
│
├── dashboard/
│   ├── server.py                   Flask UI (port 5002) — reads trades/ + MQTT
│   ├── db.py                       SQLite trade history
│   └── templates/
│       └── index.html
│
├── trades/
│   ├── active/                     Live trade JSON (per strategy namespace)
│   ├── closed/                     Completed trades
│   ├── history/                    Daily archive
│   └── commands/                   Dashboard command files
│
└── tests/
    ├── unit/
    │   ├── test_breach.py          Breach detection (credit + debit formulas)
    │   ├── test_stop_profile.py    PhaseRule evaluation
    │   ├── test_entry_config.py    Config validation
    │   └── test_state.py           JSON schema + serialization
    │
    ├── integration/
    │   └── adhoc_integration.py
    │
    └── fixtures/
        └── *.json                  Sample trade states for testing
```

---

## Part 7: Migration Path (V1 → V2)

### 7.1 What Can Be Directly Ported

| V1 Component | V2 Destination | Effort |
|-------------|----------------|--------|
| `stop_monitor/monitor.py` (core logic) | `blocks/stop/stop_block.py` | Refactor — extract strategy-specific code into StopProfile |
| `stop_monitor/breach.py` | `blocks/stop/breach.py` | Extend with debit formula |
| `stop_monitor/state.py` | `blocks/common/state.py` | Generalize schema, add `spread_type` / `strategy` fields |
| `stop_monitor/fill_sync.py` | `blocks/stop/fill_sync.py` | Direct port |
| `stop_monitor/alerts.py` | `blocks/stop/alerts.py` | Direct port |
| `stop_monitor/phases.py` | `strategies/meic/stop_profile.py` | Convert Phase1/2/3 classes to PhaseRule data |
| `open_spread_tt.py` | `blocks/entry/credit_spread.py` | Refactor — extract config into CreditEntryConfig |
| `vertical_thin.py` | Absorbed into Orchestrator + CreditSpreadEntry | Tranche-per-thread logic moves to Orchestrator |
| `streaming/publish_tastytrade.py` | `blocks/streamer/streamer_block.py` | Minor refactor — add health heartbeat, configurable close time |
| `brokers/` | `brokers/` | Direct port (BrokerBase + TastyTradeBroker are already clean) |
| `common/option_ticks.py` | `blocks/common/option_ticks.py` | Direct port |
| `common/symbols.py` | `blocks/common/symbols.py` | Direct port |
| `dashboard/` | `dashboard/` | Mostly unchanged — add strategy filter to trade grid |
| `meic0dte/app/config.py` | `strategies/meic/config.py` + `blocks/entry/entry_config.py` | Split monolithic config into strategy-specific + block-specific |

### 7.2 What Requires Significant Rework

| Component | Why | Effort |
|-----------|-----|--------|
| Phase system | V1 phases are classes with `should_activate()` + `execute()` coupled to `StopMonitor` methods. V2 phases are data (PhaseRule) with generic conditions and actions | Medium — mostly restructuring, logic is identical |
| State schema | V1 state is MEIC-specific (`two_x_short`, `short_stoplmt_replaced`). V2 must be strategy-agnostic with strategy-specific data in a nested `strategy_data` dict | Medium — schema migration |
| Orchestrator | V1 `run.py` hardcodes MEIC tranches and spawns `app_main.py`. V2 Orchestrator dynamically loads strategies from YAML | Medium — new code, but pattern is straightforward |
| Debit spread support in Stop block | V1 breach logic assumes `spread_price >= threshold` (credit). V2 must also handle `spread_price <= threshold` (debit) | Low — parameterized via StopProfile |

### 7.3 What Can Be Deferred

| Feature | Why defer |
|---------|----------|
| Remove MQTT (Goal 3 from V1) | MQTT works fine. Removing it is a separate architectural decision. V2 can start with MQTT and migrate to internal queues later |
| Multi-ticker (SPX + NDX + RUT) | Requires instrument registry. V2's `instrument` field in strategy and state supports it, but implementation is a separate effort |
| Backtesting integration | V2's block architecture makes backtesting easier (mock PriceFeed + BrokerBase), but building the backtester is out of scope |

---

## Part 8: Testing Strategy for V2

### 8.1 Unit Tests (Offline, No Broker)

| Test Suite | What it validates | Dependencies |
|------------|------------------|--------------|
| `test_breach.py` | `spread_mark_price()` and `spread_breach_triggered()` for both credit and debit directions | None |
| `test_stop_profile.py` | MEIC PhaseRules evaluate correctly given mock state and prices | None |
| `test_entry_config.py` | Config validation rejects invalid values (negative credit, inverted ranges) | None |
| `test_state.py` | JSON round-trip, atomic save/load, schema defaults | Filesystem only |
| `test_long_chase.py` | Chase loop reprices correctly, escalates to market after N attempts | Mock broker |
| `test_strike_scanner.py` | Candidate generation produces correct (short, long) pairs for P and C | None |

### 8.2 Integration Tests (Paper Account)

| Test | What it validates |
|------|------------------|
| `test_entry_to_stop_handoff` | Entry block writes JSON → Stop block picks it up and places exchange stop |
| `test_full_lifecycle` | Entry → fill → stop placement → breach → short close → long chase → finalized |
| `test_phase_transitions` | Phase 1 → 2 → 3 fire at correct conditions |
| `test_dashboard_kill` | Dashboard writes killswitch.json → Stop block force-closes |
| `test_multi_strategy` | Two strategies run simultaneously on different tranches without interference |

### 8.3 Regression Tests (V1 Parity)

Every V1 GAP fix must have a corresponding V2 test proving the same behavior:

| GAP | V2 Test |
|-----|---------|
| GAP-01 (Long close tracked) | `assert state.long_close_order_id is not None when status == 'closing'` |
| GAP-02 (Fast/slow split) | `assert breach_check_interval <= 3s and broker_sync_interval >= 60s` |
| GAP-03 (Chase loop) | `assert long_close_repriced_after_stale and market_order_after_10_attempts` |
| GAP-04 (Alert re-registration) | `assert alert_listener.registered_ids == {current_stop_order_id}` |
| GAP-07 (Session refresh) | `assert session.validate() called within last 20 minutes` |
| GAP-08 (Central time) | `assert all time checks use central_time() not datetime.now()` |
| GAP-10 (Broker retry) | `assert transient_errors_retried_3_times_with_backoff` |

---

## Part 9: Risk Analysis

### 9.1 Risks of the Rewrite

| Risk | Severity | Mitigation |
|------|----------|------------|
| **Logic regression** — V2 subtly differs from V1's battle-tested stop math | High | Side-by-side tests: feed same inputs to V1 and V2, assert identical outputs. Run both on paper simultaneously for 2 weeks |
| **Over-abstraction** — blocks become so generic they're hard to debug | Medium | Keep block code straightforward. StopProfile is data, not inheritance. Debug by reading the profile + block together |
| **New bugs in new code** — V2 is a rewrite, not a refactor | High | Port V1 code into blocks with minimal changes to logic. The refactoring is structural (where code lives), not behavioral (what it does). Lock down with tests first |
| **Dashboard breaks** — V2 state schema differs from V1 | Low | Dashboard reads JSON files. Add a compatibility layer or migrate dashboard incrementally |
| **Two codebases** — V1 and V2 coexist during transition | Medium | V2 is a separate repo. V1 stays in production until V2 passes all regression tests on paper. Clean cutover, not gradual migration |

### 9.2 What Could Go Wrong in Production

| Scenario | V1 handling | V2 handling |
|----------|------------|------------|
| Stop block crash | stop_monitor restarts (GAP-09). Exchange stop at broker protects | Same — Orchestrator health-checks stop block process |
| Streamer crash | run.py restarts streamer (GAP-09). Stop block freezes breach checks on stale data | Same — plus formal staleness guard |
| Two strategies breach simultaneously | N/A (single strategy) | Stop block uses per-trade threads for breach response (V1 pattern). Strategies are isolated by trade namespace |
| Strategy config error (credit_min > credit_max) | Silent — scans find nothing, logs "no suitable credit" | Config validation at load time. Fail fast with clear error |

---

## Part 10: Decisions Needed Before Coding

| # | Question | Options | Recommendation |
|---|----------|---------|----------------|
| 1 | Keep MQTT or move to internal queues? | (A) Keep MQTT for V2 launch, remove later. (B) Remove in V2 from day 1 | **A** — MQTT works, derisks the rewrite |
| 2 | Single process or multi-process? | (A) Streamer + Orchestrator + Stop in one process. (B) Streamer as subprocess, everything else in main | **B** — Streamer isolation prevents DXLink crashes from affecting stop logic |
| 3 | Python-only strategy definition or YAML+Python? | (A) Strategy = Python class only. (B) YAML for config, Python for logic | **B** — YAML for enable/disable + simple config overrides, Python for behavior |
| 4 | Port dashboard to V2 schema from day 1? | (A) Yes, update dashboard for new state schema. (B) Add compat shim | **A** — clean break in a new repo |
| 5 | Use asyncio or threading? | (A) Threading (V1 pattern). (B) asyncio throughout | **A for launch** — V1's threading works and is debuggable. Asyncio is a future optimization |
| 6 | Stop block: one per trade or one supervisor with trade loop? | (A) One StopBlock instance per trade (V1 pattern). (B) Single supervisor iterating all trades | **A** — isolation is worth the thread overhead at 12 trades max |
| 7 | `strategies/` folder per strategy or flat? | (A) Nested (`strategies/meic/`). (B) Flat (`strategies/meic_strategy.py`) | **A** — each strategy may have its own config, tests, stop profile |
| 8 | Debit Spread Entry: build now or stub? | (A) Full implementation. (B) Interface + stub, implement when needed | **B** — no active debit strategy yet |

---

## Part 11: Summary of Blocks → MEIC Wiring

How MEIC uses each block as a LEGO piece:

```
MEIC Strategy Definition
│
├── Entry: CreditSpreadEntry
│     ├── credit_min = 0.90
│     ├── credit_max = 1.85
│     ├── spread_width = 25–35
│     ├── quantity = 1
│     └── Uses: Streamer (for MQTT prices during scan)
│              Broker (to place NET_CREDIT order)
│
├── Stop: StopBlock with MEIC StopProfile
│     ├── initial_stop = (short_fill - 0.10) × 2.0
│     ├── Phase 1: Monitor breach (spread_mid >= 2x_short + 0.20)
│     ├── Phase 2: When long ≤ $0.05 → switch to 2x_net_credit
│     ├── Phase 3: At 14:51, if SPX within 3 pts → market close
│     ├── Long close: 30s delay, chase at mid, market after 10 attempts
│     └── Uses: Streamer (for MQTT prices every 3s)
│              Broker (for stop placement, cancel, limit close)
│              AlertListener (for instant fill detection)
│
├── Schedule: 6 tranches × 2 sides = 12 potential trades/day
│     ├── 11:00, 12:00, 12:30, 1:15, 1:45, 2:00 CT
│     └── Each: Put spread + Call spread
│
└── Shared Infrastructure:
      ├── Streamer: DXLink → MQTT (SPX + option symbols)
      ├── Broker: TastyTrade (single session, retry, refresh)
      └── Dashboard: Flask UI reading trades/ + MQTT
```

---

*Last updated: Jun 21, 2026 — analysis complete. No code changes. This document is the blueprint for the V2 separate repo.*
