from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Optional

Side = Literal['long', 'short']
Outcome = Literal['win', 'loss', 'flattened']
ExitReason = Literal['target', 'stop', 'flatten']
QuoteSource = Literal['rest', 'quote', 'trade']


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def round_tick(price: float, tick: float = 0.25) -> float:
    if tick <= 0:
        return price
    return round(round(price / tick) * tick, 4)


@dataclass
class LiveQuote:
    symbol: str
    streamer_symbol: str
    bid: Optional[float] = None
    ask: Optional[float] = None
    last: Optional[float] = None
    mid: Optional[float] = None
    ts: float = 0.0
    source: QuoteSource = 'rest'

    def market_fill(self, side: Side) -> Optional[float]:
        """Paper market fill: buy the ask, sell the bid, else last/mid."""
        if side == 'long':
            for val in (self.ask, self.last, self.mid, self.bid):
                if val is not None and val > 0:
                    return float(val)
        else:
            for val in (self.bid, self.last, self.mid, self.ask):
                if val is not None and val > 0:
                    return float(val)
        return None

    def mark(self) -> Optional[float]:
        if self.source == 'trade' and self.last is not None and self.last > 0:
            return float(self.last)
        for val in (self.mid, self.last, self.bid, self.ask):
            if val is not None and val > 0:
                return float(val)
        if self.bid and self.ask and self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return None


@dataclass
class OpenTrade:
    id: str
    symbol: str
    streamer_symbol: str
    side: Side
    qty: int
    entry: float
    stop: float
    target: float
    stop_points: float
    target_points: float
    signal_received_at: str
    opened_at: str
    entry_source: str
    signal: dict[str, Any] = field(default_factory=dict)
    note: str = ''
    paper: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OpenTrade:
        return cls(
            id=str(data['id']),
            symbol=str(data['symbol']),
            streamer_symbol=str(data.get('streamer_symbol') or data['symbol']),
            side=data['side'],
            qty=int(data.get('qty') or 1),
            entry=float(data['entry']),
            stop=float(data['stop']),
            target=float(data['target']),
            stop_points=float(data.get('stop_points') or 0),
            target_points=float(data.get('target_points') or 0),
            signal_received_at=str(
                data.get('signal_received_at') or data.get('opened_at') or utc_now_iso()
            ),
            opened_at=str(data.get('opened_at') or utc_now_iso()),
            entry_source=str(data.get('entry_source') or 'rest'),
            signal=dict(data.get('signal') or {}),
            note=str(data.get('note') or ''),
            paper=bool(data.get('paper', True)),
        )


@dataclass
class ClosedTrade:
    id: str
    symbol: str
    streamer_symbol: str
    side: Side
    qty: int
    entry: float
    stop: float
    target: float
    exit: float
    outcome: Outcome
    reason: ExitReason
    pnl_points: float
    pnl_usd: float
    signal_received_at: str
    opened_at: str
    closed_at: str
    entry_source: str
    exit_source: str
    signal: dict[str, Any] = field(default_factory=dict)
    note: str = ''
    paper: bool = True
    success: bool = field(init=False)

    def __post_init__(self) -> None:
        self.success = self.outcome == 'win'

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['success'] = self.success
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClosedTrade:
        return cls(
            id=str(data['id']),
            symbol=str(data['symbol']),
            streamer_symbol=str(data.get('streamer_symbol') or data['symbol']),
            side=data['side'],
            qty=int(data.get('qty') or 1),
            entry=float(data['entry']),
            stop=float(data['stop']),
            target=float(data['target']),
            exit=float(data['exit']),
            outcome=data['outcome'],
            reason=data['reason'],
            pnl_points=float(data.get('pnl_points') or 0),
            pnl_usd=float(data.get('pnl_usd') or 0),
            signal_received_at=str(
                data.get('signal_received_at') or data.get('opened_at') or utc_now_iso()
            ),
            opened_at=str(data.get('opened_at') or utc_now_iso()),
            closed_at=str(data.get('closed_at') or utc_now_iso()),
            entry_source=str(data.get('entry_source') or 'rest'),
            exit_source=str(data.get('exit_source') or 'streamer'),
            signal=dict(data.get('signal') or {}),
            note=str(data.get('note') or ''),
            paper=bool(data.get('paper', True)),
        )

