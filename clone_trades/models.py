"""Data models for Discretionary Clone Trades."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any, Dict, List, Optional
import uuid


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_cst(iso_str: Optional[str]) -> Optional[str]:
    """Convert an ISO-8601 UTC string (e.g. 2026-10-13T14:29:15Z) to CST/CDT representation.
    Returns: 'YYYY-MM-DD HH:MM:SS CST' (or CDT)
    """
    if not iso_str:
        return None
    try:
        clean = iso_str.replace("Z", "+00:00")
        dt_utc = datetime.fromisoformat(clean)
        try:
            import zoneinfo
            tz_central = zoneinfo.ZoneInfo("America/Chicago")
            dt_ct = dt_utc.astimezone(tz_central)
            tz_name = dt_ct.tzname() or "CST"
            return dt_ct.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
        except Exception:
            from meic0dte.app.utilities import _central_offset_for_utc
            from datetime import timedelta
            offset = _central_offset_for_utc(dt_utc)
            dt_ct = dt_utc + timedelta(hours=offset)
            tz_name = "CDT" if offset == -5 else "CST"
            return dt_ct.strftime(f"%Y-%m-%d %H:%M:%S {tz_name}")
    except Exception:
        return iso_str


def iso_to_date_cst(iso_str: Optional[str]) -> Optional[str]:
    """Extract YYYY-MM-DD in CST/CDT from ISO UTC timestamp."""
    cst_str = iso_to_cst(iso_str)
    if cst_str and len(cst_str) >= 10:
        return cst_str[:10]
    return None


@dataclass
class OptionSpec:
    underlying: str
    expiration: str  # YYYY-MM-DD
    strike: float
    option_type: str  # 'CALL' or 'PUT'
    occ_symbol: str
    streamer_symbol: str

    @classmethod
    def build(cls, underlying: str, exp_str: str, strike: float, option_type: str) -> OptionSpec:
        und = underlying.upper().strip()
        ot = option_type.upper().strip()
        ot_char = "C" if ot.startswith("C") else "P"
        norm_ot = "CALL" if ot_char == "C" else "PUT"

        # Normalize expiration date to YYYY-MM-DD and YYMMDD
        clean_exp = re.sub(r"[^\d]", "", exp_str.strip())
        if len(clean_exp) == 8:  # YYYYMMDD
            yyyy = int(clean_exp[:4])
            mm = int(clean_exp[4:6])
            dd = int(clean_exp[6:8])
            exp_iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
            yymmdd = f"{yyyy % 100:02d}{mm:02d}{dd:02d}"
        elif len(clean_exp) == 6:  # YYMMDD
            yy = int(clean_exp[:2])
            yyyy = 2000 + yy if yy < 70 else 1900 + yy
            mm = int(clean_exp[2:4])
            dd = int(clean_exp[4:6])
            exp_iso = f"{yyyy:04d}-{mm:02d}-{dd:02d}"
            yymmdd = f"{yy:02d}{mm:02d}{dd:02d}"
        else:
            # Try parsing with datetime if standard ISO string
            try:
                dt = datetime.fromisoformat(exp_str.strip())
                exp_iso = dt.strftime("%Y-%m-%d")
                yymmdd = dt.strftime("%y%m%d")
            except Exception:
                raise ValueError(f"Cannot parse expiration date: {exp_str}")

        # Root symbol determination for OCC and streamer:
        # In US options exchanges (CBOE, TastyTrade), 0DTE/daily/weekly SPX contracts
        # trade under root symbol 'SPXW' (PM-settled). 'SPX' is strictly reserved for monthly AM-settled.
        # If underlying is SPX (or SPXW), route to SPXW for broker OCC & streamer symbols.
        root = und
        if und in ("SPX", "SPXW"):
            root = "SPXW"

        # OCC format: Root (6 characters padded with spaces) + YYMMDD + Type + Strike (8 digits, *1000)
        root_padded = root.ljust(6)
        strike_val = float(strike)
        strike_int = int(round(strike_val * 1000))
        occ_symbol = f"{root_padded}{yymmdd}{ot_char}{strike_int:08d}"

        # Streamer symbol: .ROOTYYMMDD[C|P]STRIKE
        strike_disp = f"{strike_val:.1f}" if strike_val % 1 else f"{int(strike_val)}"
        streamer_symbol = f".{root}{yymmdd}{ot_char}{strike_disp}"

        return cls(
            underlying=und,
            expiration=exp_iso,
            strike=strike_val,
            option_type=norm_ot,
            occ_symbol=occ_symbol,
            streamer_symbol=streamer_symbol,
        )

    @classmethod
    def from_occ_or_streamer(cls, symbol_str: str) -> Optional[OptionSpec]:
        """Parse from OCC format e.g. SPY   260920C00500000 or .SPY260920C500."""
        text = symbol_str.strip()
        # Check standard 21-char OCC symbol
        occ_match = re.match(r"^([A-Z\s]{6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$", text.upper())
        if occ_match:
            und = occ_match.group(1).strip()
            yy, mm, dd = occ_match.group(2), occ_match.group(3), occ_match.group(4)
            ot = occ_match.group(5)
            strike = int(occ_match.group(6)) / 1000.0
            return cls.build(und, f"20{yy}-{mm}-{dd}", strike, ot)

        # Check streamer symbol e.g. .SPY260920C500 or SPY260920C500
        clean = text.lstrip(".")
        sm_match = re.match(r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])([\d\.]+)$", clean.upper())
        if sm_match:
            und = sm_match.group(1)
            yy, mm, dd = sm_match.group(2), sm_match.group(3), sm_match.group(4)
            ot = sm_match.group(5)
            strike = float(sm_match.group(6))
            return cls.build(und, f"20{yy}-{mm}-{dd}", strike, ot)
        return None

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> OptionSpec:
        """Parse option spec from various JSON webhook formats."""
        if not isinstance(payload, dict):
            raise ValueError("Payload must be a JSON object")

        # 1. Try if symbol or ticker itself is an OCC/streamer symbol
        for key in ("ticker", "symbol", "contract", "option"):
            raw_sym = str(payload.get(key) or "").strip()
            if raw_sym:
                parsed = cls.from_occ_or_streamer(raw_sym)
                if parsed:
                    return parsed

        # 2. Extract components
        und = ""
        for key in ("ticker", "symbol", "underlying", "root"):
            val = str(payload.get(key) or "").strip()
            if val:
                und = val
                break
        if not und:
            raise ValueError("Missing 'ticker' or 'underlying' in payload")

        exp = ""
        for key in ("exp", "exp_date", "expiration", "expiry", "date"):
            val = str(payload.get(key) or "").strip()
            if val:
                exp = val
                break
        if not exp:
            raise ValueError("Missing 'expiration' / 'exp_date' in payload")

        strike_raw = None
        for key in ("strike", "strike_price", "k"):
            if key in payload and payload[key] is not None:
                strike_raw = payload[key]
                break
        if strike_raw is None:
            raise ValueError("Missing 'strike' in payload")
        try:
            strike = float(str(strike_raw).replace(",", "").strip())
        except ValueError:
            raise ValueError(f"Invalid strike price: {strike_raw}")

        ot_raw = ""
        for key in ("type", "option_type", "call_put", "right", "side"):
            val = str(payload.get(key) or "").strip()
            if val and val.lower() not in ("buy", "sell", "long", "short"):
                ot_raw = val
                break
        if not ot_raw:
            # Check if action or direction implies call/put (default to CALL if unspecified)
            action = str(payload.get("action") or payload.get("direction") or "").lower()
            if "put" in action or "bear" in action:
                ot_raw = "PUT"
            elif "call" in action or "bull" in action:
                ot_raw = "CALL"
            else:
                ot_raw = "CALL"

        return cls.build(und, exp, strike, ot_raw)


@dataclass
class LadderTier:
    tier_id: int
    target_profit_pct: float  # e.g. 25.0 for +25%
    stop_loss_pct: float  # e.g. -50.0 for -50% loss, 0.0 for breakeven, 50.0 for +50% profit lock
    description: str = ""
    exit_pct: float = 0.0  # Percentage of trades/contracts taken off at this profit target stage (e.g. 33.3%)

    def __post_init__(self):
        # Handle if exit_pct and description were passed in reverse
        if isinstance(self.description, (int, float)) and isinstance(self.exit_pct, str):
            self.description, self.exit_pct = self.exit_pct, float(self.description)
        try:
            self.exit_pct = float(self.exit_pct)
        except (ValueError, TypeError):
            self.exit_pct = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> LadderTier:
        return cls(
            tier_id=int(data.get("tier_id", 1)),
            target_profit_pct=float(data.get("target_profit_pct", 25.0)),
            stop_loss_pct=float(data.get("stop_loss_pct", -50.0)),
            description=str(data.get("description", "")),
            exit_pct=float(data.get("exit_pct", data.get("contracts_pct", 0.0))),
        )


@dataclass
class ClosedLeg:
    tier_id: int
    quantity: int
    exit_price: float
    exit_time: str
    profit_pct: float
    pnl_usd: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["exit_time_cst"] = iso_to_cst(self.exit_time)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> ClosedLeg:
        raw = dict(data)
        raw.pop("exit_time_cst", None)
        return cls(
            tier_id=int(raw.get("tier_id", 1)),
            quantity=int(raw.get("quantity", 1)),
            exit_price=float(raw.get("exit_price", 0.0)),
            exit_time=str(raw.get("exit_time", "")),
            profit_pct=float(raw.get("profit_pct", 0.0)),
            pnl_usd=float(raw.get("pnl_usd", 0.0)),
            reason=str(raw.get("reason", "")),
        )


def extract_called_by(payload: Dict[str, Any]) -> str:
    """Extract 'calledBy' / 'called_by' / 'Called By' from payload or empty string if missing."""
    if not isinstance(payload, dict):
        return ""
    for key in ("calledBy", "called_by", "Called By", "calledby", "caller", "source"):
        val = payload.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return ""


def classify_sub_strategy(called_by: Optional[str]) -> str:
    """Classify incoming called_by string into sub-strategy bucket:
    - 'TG': TG (case-insensitive, trimmed)
    - 'PC': PC (case-insensitive, trimmed)
    - 'PC2': PC2 (case-insensitive, trimmed)
    - 'Genie': Genie (case-insensitive, trimmed)
    - 'Cowboy': Cowboy (case-insensitive, trimmed)
    - 'Others': Missing key, empty string, or any other value
    """
    if not called_by:
        return "Others"
    clean = str(called_by).strip().lower().replace(" ", "").replace("_", "").replace("-", "")
    if clean == "tg":
        return "TG"
    if clean == "pc":
        return "PC"
    if clean == "pc2":
        return "PC2"
    if clean == "genie":
        return "Genie"
    if clean == "cowboy":
        return "Cowboy"
    if clean in ("aiflowalgos", "aiflowalgo"):
        return "Genie"
    if clean in ("humanalerters", "humanalerter"):
        return "PC"
    if clean == "others":
        return "Others"
    return "Others"


@dataclass
class CloneTrade:
    id: str
    created_at: str
    spec: OptionSpec
    action: str  # 'BUY'
    webhook_price: float  # Alert price passed in webhook (per share)
    current_price_at_signal: float  # Market price at signal arrival (per share)
    premium_pct_at_signal: float  # (current - webhook) / webhook * 100
    entry_price: float  # Fill price per share
    contract_cost: float  # entry_price * 100
    initial_quantity: int  # Initial contracts entered
    remaining_quantity: int  # Current open contracts
    current_price: float  # Latest market price per share
    highest_price: float  # Highest market price seen (Peak)
    highest_profit_pct: float
    status: str  # 'OPEN', 'CLOSED', 'CANCELLED'
    status_reason: str
    current_tier_index: int  # 0-based index into ladder_tiers for next PT
    active_pt_price: float  # Profit target price for current ladder step
    active_stop_price: float  # Stop loss price for all remaining contracts
    active_stop_pct: float  # Current active SL percentage
    tier_distribution: List[Dict[str, Any]]  # Contract distribution per tier
    closed_legs: List[ClosedLeg] = field(default_factory=list)
    realized_pnl_usd: float = 0.0
    unrealized_pnl_pct: float = 0.0
    unrealized_pnl_usd: float = 0.0
    order_id: Optional[str] = None
    close_order_id: Optional[str] = None
    closed_at: Optional[str] = None
    called_by: str = ""
    sub_strategy: str = "Others"
    paper_mode: bool = False
    ladder_tiers: List[Dict[str, Any]] = field(default_factory=list)
    lowest_price: float = 0.0  # Lowest market price seen (Bottom)
    entry_placed_time: float = 0.0
    last_chase_time: float = 0.0
    chase_count: int = 0
    max_chase_attempts: int = 3
    is_flow_alt: bool = False
    parent_signal_id: Optional[str] = None
    is_roll_runner: bool = False
    roll_count: int = 0
    parent_trade_id: Optional[str] = None
    original_entry_price: float = 0.0
    roll_runner_enabled: bool = False
    roll_runner_qty: int = 1
    roll_runner_every_leg: bool = False

    def __post_init__(self):
        if self.lowest_price <= 0.0 and self.entry_price > 0.0:
            self.lowest_price = self.entry_price
        if getattr(self, "original_entry_price", 0.0) <= 0.0 and self.entry_price > 0.0:
            self.original_entry_price = self.entry_price
        if getattr(self, "realized_pnl_usd", 0.0) == 0.0 and self.closed_legs:
            sum_legs = sum(getattr(l, "pnl_usd", 0.0) for l in self.closed_legs)
            if sum_legs != 0.0:
                self.realized_pnl_usd = round(sum_legs, 2)

    @property
    def created_at_cst(self) -> Optional[str]:
        return iso_to_cst(self.created_at)

    @property
    def closed_at_cst(self) -> Optional[str]:
        return iso_to_cst(self.closed_at)

    @property
    def date_cst(self) -> Optional[str]:
        return iso_to_date_cst(self.created_at)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["closed_legs"] = [leg.to_dict() if isinstance(leg, ClosedLeg) else leg for leg in self.closed_legs]
        d["created_at_cst"] = self.created_at_cst
        d["closed_at_cst"] = self.closed_at_cst
        d["date_cst"] = self.date_cst
        d["called_by"] = self.called_by
        d["sub_strategy"] = self.sub_strategy
        d["paper_mode"] = self.paper_mode
        d["ladder_tiers"] = self.ladder_tiers
        d["lowest_price"] = self.lowest_price
        d["is_flow_alt"] = self.is_flow_alt
        d["parent_signal_id"] = self.parent_signal_id
        d["is_roll_runner"] = self.is_roll_runner
        d["roll_count"] = self.roll_count
        d["parent_trade_id"] = self.parent_trade_id
        d["original_entry_price"] = self.original_entry_price
        d["roll_runner_enabled"] = self.roll_runner_enabled
        d["roll_runner_qty"] = self.roll_runner_qty
        d["roll_runner_every_leg"] = self.roll_runner_every_leg
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CloneTrade:
        raw = dict(data)
        raw.pop("created_at_cst", None)
        raw.pop("closed_at_cst", None)
        raw.pop("date_cst", None)
        spec_data = raw.pop("spec")
        spec = OptionSpec(**spec_data) if isinstance(spec_data, dict) else spec_data
        raw_legs = raw.pop("closed_legs", [])
        legs = [ClosedLeg.from_dict(l) if isinstance(l, dict) else l for l in raw_legs]

        called_by = raw.pop("called_by", None)
        sub_strategy = raw.pop("sub_strategy", None)
        if called_by:
            caller_bucket = classify_sub_strategy(called_by)
            if caller_bucket in ("TG", "PC", "PC2", "Cowboy", "Genie", "AI FlowAlgos"):
                sub_strategy = caller_bucket
        if not sub_strategy or sub_strategy == "Human Alerters":
            sub_strategy = "PC"
        elif sub_strategy == "AI FlowAlgos":
            sub_strategy = "Cowboy" if (called_by and "cowboy" in str(called_by).lower()) else "Genie"
        if not called_by:
            called_by = sub_strategy if sub_strategy in ("TG", "PC", "PC2", "Cowboy", "Genie") else "Others"

        paper_mode = bool(raw.pop("paper_mode", False))
        ladder_tiers = raw.pop("ladder_tiers", [])
        lowest_price = float(raw.pop("lowest_price", raw.get("entry_price", 0.0)))
        is_flow_alt = bool(raw.pop("is_flow_alt", False))
        parent_signal_id = raw.pop("parent_signal_id", None)
        is_roll_runner = bool(raw.pop("is_roll_runner", False))
        roll_count = int(raw.pop("roll_count", 0))
        parent_trade_id = raw.pop("parent_trade_id", None)
        original_entry_price = float(raw.pop("original_entry_price", raw.get("entry_price", 0.0)))
        roll_runner_enabled = bool(raw.pop("roll_runner_enabled", False))
        roll_runner_qty = int(raw.pop("roll_runner_qty", 1))
        roll_runner_every_leg = bool(raw.pop("roll_runner_every_leg", False))

        return cls(
            spec=spec,
            closed_legs=legs,
            called_by=called_by,
            sub_strategy=sub_strategy,
            paper_mode=paper_mode,
            ladder_tiers=ladder_tiers,
            lowest_price=lowest_price,
            is_flow_alt=is_flow_alt,
            parent_signal_id=parent_signal_id,
            is_roll_runner=is_roll_runner,
            roll_count=roll_count,
            parent_trade_id=parent_trade_id,
            original_entry_price=original_entry_price,
            roll_runner_enabled=roll_runner_enabled,
            roll_runner_qty=roll_runner_qty,
            roll_runner_every_leg=roll_runner_every_leg,
            **raw
        )


@dataclass
class CloneSignal:
    id: str
    received_at: str
    raw_payload: Dict[str, Any]
    spec: Optional[OptionSpec]
    alert_price: float
    market_price: Optional[float] = None
    premium_pct: Optional[float] = None
    status: str = "PENDING"  # 'TRACKING', 'TAKEN', 'SKIPPED', 'CHASE_EXCEEDED', 'FLOOR_EXCEEDED', 'DISMISSED_MANUAL', 'REJECTED_ERROR'
    status_reason: str = ""
    trade_id: Optional[str] = None
    manual_decision: Optional[Dict[str, Any]] = None
    called_by: str = ""
    sub_strategy: str = "Others"
    current_price: Optional[float] = None
    peak_price: Optional[float] = None
    bottom_price: Optional[float] = None
    last_quote_time: Optional[str] = None
    parent_signal_id: Optional[str] = None
    is_flow_alt: bool = False
    alt_signal_id: Optional[str] = None

    @property
    def received_at_cst(self) -> Optional[str]:
        return iso_to_cst(self.received_at)

    @property
    def date_cst(self) -> Optional[str]:
        return iso_to_date_cst(self.received_at)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["received_at_cst"] = self.received_at_cst
        d["date_cst"] = self.date_cst
        d["called_by"] = self.called_by
        d["sub_strategy"] = self.sub_strategy
        d["current_price"] = self.current_price
        d["peak_price"] = self.peak_price
        d["bottom_price"] = self.bottom_price
        d["last_quote_time"] = self.last_quote_time
        d["parent_signal_id"] = self.parent_signal_id
        d["is_flow_alt"] = self.is_flow_alt
        d["alt_signal_id"] = self.alt_signal_id
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CloneSignal:
        raw = dict(data)
        raw.pop("received_at_cst", None)
        raw.pop("date_cst", None)
        spec_data = raw.pop("spec", None)
        spec = OptionSpec(**spec_data) if isinstance(spec_data, dict) else None

        raw_payload = raw.get("raw_payload") or {}
        called_by = raw.pop("called_by", None)
        sub_strategy = raw.pop("sub_strategy", None)
        if not sub_strategy:
            if isinstance(raw_payload, dict):
                caller = extract_called_by(raw_payload)
                if caller:
                    called_by = caller
                    sub_strategy = classify_sub_strategy(caller)
                else:
                    called_by = "Others"
                    sub_strategy = "Others"
            else:
                called_by = "Others"
                sub_strategy = "Others"
        elif called_by:
            caller_bucket = classify_sub_strategy(called_by)
            if caller_bucket in ("TG", "PC", "PC2", "Cowboy", "Genie", "AI FlowAlgos"):
                sub_strategy = caller_bucket
            elif sub_strategy == "Human Alerters":
                sub_strategy = "PC"
        elif sub_strategy == "Human Alerters":
            sub_strategy = "PC"
        elif sub_strategy == "AI FlowAlgos":
            sub_strategy = "Cowboy" if (called_by and "cowboy" in str(called_by).lower()) else "Genie"

        if not called_by:
            called_by = sub_strategy if sub_strategy in ("TG", "PC", "PC2", "Cowboy", "Genie") else "Others"

        current_price = raw.pop("current_price", None)
        peak_price = raw.pop("peak_price", None)
        bottom_price = raw.pop("bottom_price", None)
        last_quote_time = raw.pop("last_quote_time", None)
        parent_signal_id = raw.pop("parent_signal_id", None)
        is_flow_alt = bool(raw.pop("is_flow_alt", False))
        alt_signal_id = raw.pop("alt_signal_id", None)

        return cls(
            spec=spec,
            called_by=called_by,
            sub_strategy=sub_strategy,
            current_price=current_price,
            peak_price=peak_price,
            bottom_price=bottom_price,
            last_quote_time=last_quote_time,
            parent_signal_id=parent_signal_id,
            is_flow_alt=is_flow_alt,
            alt_signal_id=alt_signal_id,
            **raw
        )


def distribute_contracts_across_tiers(
    total_contracts: int,
    num_tiers: int,
    tier_pcts: Optional[List[float]] = None,
) -> List[int]:
    """Distribute total contracts across ladder tiers based on percentage weights or evenly.
    E.g.: 3 contracts, 3 tiers (33.3% each) -> [1, 1, 1]
          5 contracts, 3 tiers [50%, 25%, 25%] -> [3, 1, 1]
          1 contract, 3 tiers -> [1, 0, 0]
          2 contracts, 3 tiers -> [1, 1, 0]
    """
    if total_contracts <= 0:
        return [0] * num_tiers
    if num_tiers <= 1:
        return [total_contracts]

    safe_pcts = []
    for p in (tier_pcts or []):
        try:
            safe_pcts.append(float(p))
        except (ValueError, TypeError):
            safe_pcts.append(0.0)

    # If tier_pcts is not provided or all zero, use balanced even distribution
    if not safe_pcts or all(p <= 0 for p in safe_pcts):
        base = total_contracts // num_tiers
        rem = total_contracts % num_tiers
        res = []
        for i in range(num_tiers):
            qty = base + (1 if i < rem else 0)
            res.append(qty)
        return res

    # Use specified tier percentages
    norm_pcts = list(safe_pcts)
    if len(norm_pcts) < num_tiers:
        norm_pcts += [0.0] * (num_tiers - len(norm_pcts))
    norm_pcts = norm_pcts[:num_tiers]

    total_pct = sum(norm_pcts)
    if total_pct > 0:
        weights = [p / total_pct for p in norm_pcts]
    else:
        weights = [1.0 / num_tiers] * num_tiers

    if total_contracts == 1:
        res = [0] * num_tiers
        first_idx = next((i for i, w in enumerate(weights) if w > 0), 0)
        res[first_idx] = 1
        return res

    # Hare-Niemeyer (largest remainder) distribution
    raw_alloc = [w * total_contracts for w in weights]
    res = [int(math.floor(a)) for a in raw_alloc]
    rem_contracts = total_contracts - sum(res)
    remainders = sorted(
        [(raw_alloc[i] - res[i], i) for i in range(num_tiers)],
        key=lambda x: x[0],
        reverse=True,
    )
    for i in range(rem_contracts):
        res[remainders[i][1]] += 1

    # Ensure tier 1 has at least 1 contract if total_contracts >= 1 and res[0] == 0
    if total_contracts >= 1 and sum(res) == total_contracts and res[0] == 0:
        max_idx = max(range(1, num_tiers), key=lambda i: res[i])
        if res[max_idx] > 1:
            res[max_idx] -= 1
            res[0] += 1

    return res
