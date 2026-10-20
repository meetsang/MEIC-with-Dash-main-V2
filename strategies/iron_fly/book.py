"""Local atomic book and state persistence for Iron Fly."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from common.symbols import to_schwab, to_tastytrade

log = logging.getLogger(__name__)


class FlyStatus:
    PENDING_APPROVAL = 'PENDING_APPROVAL'
    OPENING = 'OPENING'
    OPEN = 'OPEN'
    CLOSING = 'CLOSING'
    CLOSED = 'CLOSED'
    EXPIRED = 'EXPIRED'
    FAILED = 'FAILED'


@dataclass
class DiaryEvent:
    ts: str = ''
    type: str = ''
    msg: str = ''
    time: Optional[str] = None
    message: Optional[str] = None

    def __post_init__(self):
        if not self.ts and self.time:
            self.ts = self.time
        if not self.time and self.ts:
            self.time = self.ts
        if not self.msg and self.message:
            self.msg = self.message
        if not self.message and self.msg:
            self.message = self.msg

    def to_dict(self) -> Dict[str, Any]:
        return {
            'ts': self.ts,
            'time': self.time or self.ts,
            'type': self.type,
            'msg': self.msg,
            'message': self.message or self.msg,
        }


@dataclass
class IronFly:
    id: str
    seq: int = 1
    day: str = ''
    center: float = 0.0
    call_width: int = 0
    put_width: int = 0
    qty: int = 1
    status: str = 'OPEN'  # PENDING_APPROVAL | OPENING | OPEN | CLOSING | CLOSED | EXPIRED | FAILED
    planned_credit: float = 0.0
    fill_credit: Optional[float] = None
    be_low: Optional[float] = None
    be_high: Optional[float] = None
    max_loss: Optional[float] = None
    legs: List[Any] = field(default_factory=list)
    entry_order_id: Optional[str] = None
    exit_order_id: Optional[str] = None
    exit_debit: Optional[float] = None
    fees: float = 0.0
    realized_pnl: Optional[float] = 0.0
    unrealized_pnl: Optional[float] = 0.0
    mark_debit: Optional[float] = None
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None
    close_reason: Optional[str] = None  # cut | manual | expiry | kill | cancelled | rejected
    diary: Dict[str, Any] = field(default_factory=dict)

    # Scaled / XSP convenience aliases
    cw: Optional[int] = None
    pw: Optional[int] = None
    call_long: Optional[float] = None
    put_long: Optional[float] = None
    credit: Optional[float] = None
    quantity: Optional[int] = None
    open_risk: Optional[float] = None
    fees_paid: Optional[float] = None
    entry_fill_time: Optional[str] = None
    exit_fill_time: Optional[str] = None

    def __post_init__(self):
        if self.cw is not None:
            self.call_width = self.cw
        else:
            self.cw = self.call_width

        if self.pw is not None:
            self.put_width = self.pw
        else:
            self.pw = self.put_width

        if self.call_long is None:
            self.call_long = self.center + self.call_width

        if self.put_long is None:
            self.put_long = self.center - self.put_width

        if self.quantity is not None:
            self.qty = self.quantity
        else:
            self.quantity = self.qty

        if self.credit is not None:
            if self.fill_credit is None:
                self.fill_credit = self.credit
            if self.planned_credit == 0.0:
                self.planned_credit = self.credit
        else:
            self.credit = self.fill_credit if self.fill_credit is not None else self.planned_credit

        if self.fees_paid is not None:
            self.fees = self.fees_paid
        else:
            self.fees_paid = self.fees

        if self.open_risk is not None:
            self.max_loss = self.open_risk
        else:
            self.open_risk = self.max_loss or 0.0

        if self.unrealized_pnl is None:
            self.unrealized_pnl = 0.0
        if self.realized_pnl is None:
            self.realized_pnl = 0.0

        if self.opened_at is not None and self.entry_fill_time is None:
            self.entry_fill_time = self.opened_at
        elif self.entry_fill_time is not None and self.opened_at is None:
            self.opened_at = self.entry_fill_time

        if self.closed_at is not None and self.exit_fill_time is None:
            self.exit_fill_time = self.closed_at
        elif self.exit_fill_time is not None and self.closed_at is None:
            self.closed_at = self.exit_fill_time

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> IronFly:
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)


@dataclass
class IronFlyBook:
    day: str
    mode: str = 'PAPER'  # OFF | PAPER | LIVE_APPROVE | LIVE_AUTO
    em_open: Optional[float] = None
    em_now: Optional[float] = None
    ratio_vs_83: Optional[float] = None
    current_tier: int = 0
    first_fly_credit: Optional[float] = None
    first_hour_high: Optional[float] = None
    first_hour_low: Optional[float] = None
    spot_trail: List[Tuple[str, float]] = field(default_factory=list)
    flies: Dict[str, IronFly] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    def add_event(self, event_type: str, message: str, **kwargs) -> None:
        now_str = datetime.now().strftime('%H:%M:%S')
        evt = {'time': now_str, 'type': event_type, 'message': message, **kwargs}
        self.events.insert(0, evt)
        if len(self.events) > 200:
            self.events = self.events[:200]
        log.info('[IronFly Event] %s: %s (%s)', event_type, message, kwargs or '')

    def get_open_flies(self) -> List[IronFly]:
        """Flies currently open or closing."""
        return [f for f in self.flies.values() if f.status in ('OPEN', 'CLOSING')]

    def get_active_and_opening_flies(self) -> List[IronFly]:
        """Flies in OPEN, CLOSING, or OPENING state."""
        return [f for f in self.flies.values() if f.status in ('OPEN', 'CLOSING', 'OPENING')]

    def get_open_centers(self) -> List[float]:
        """Unique centers of open and closing flies."""
        return sorted({f.center for f in self.get_open_flies()})

    def get_short_legs_map(self) -> Dict[Tuple[str, float], int]:
        """Map of {(right, strike): net_qty} shorted across active/opening flies (§2.6a)."""
        short_map: Dict[Tuple[str, float], int] = {}
        for f in self.get_active_and_opening_flies():
            # In an iron fly, the two center legs are short: call @ center and put @ center
            short_map[('C', f.center)] = short_map.get(('C', f.center), 0) + f.qty
            short_map[('P', f.center)] = short_map.get(('P', f.center), 0) + f.qty
        return short_map

    def get_long_legs_map(self) -> Dict[Tuple[str, float], int]:
        """Map of {(right, strike): net_qty} long across active/opening flies (§2.6a)."""
        long_map: Dict[Tuple[str, float], int] = {}
        for f in self.get_active_and_opening_flies():
            # In an iron fly, the two wings are long
            c_wing = f.center + f.call_width
            p_wing = f.center - f.put_width
            long_map[('C', c_wing)] = long_map.get(('C', c_wing), 0) + f.qty
            long_map[('P', p_wing)] = long_map.get(('P', p_wing), 0) + f.qty
        return long_map

    def open_risk(self) -> float:
        """Total dollars at risk across all open flies."""
        return round(sum(f.max_loss or 0.0 for f in self.get_open_flies()), 2)

    def realized_pnl(self) -> float:
        return round(sum(f.realized_pnl or 0.0 for f in self.flies.values() if f.realized_pnl is not None), 2)

    def unrealized_pnl(self) -> float:
        return round(sum(f.unrealized_pnl or 0.0 for f in self.get_open_flies() if f.unrealized_pnl is not None), 2)

    def total_fees(self) -> float:
        return round(sum(f.fees for f in self.flies.values()), 2)

    def net_pnl(self) -> float:
        return round(self.realized_pnl() + self.unrealized_pnl() - self.total_fees(), 2)

    def to_dict(self) -> Dict[str, Any]:
        flies_dict = {fid: f.to_dict() for fid, f in self.flies.items()}
        structures_list = []
        for fid, f in self.flies.items():
            structures_list.append({
                'name': fid,
                'id': fid,
                'center': f.center,
                'status': 'open' if f.status == 'OPEN' else 'closing' if f.status == 'CLOSING' else 'closed',
                'legs': f.legs,
                'net_credit': f.fill_credit or f.planned_credit,
                'unrealized_pnl': f.unrealized_pnl,
                'closed_manually': f.close_reason == 'manual',
                'opened_at': f.opened_at or f.entry_fill_time,
                'closed_at': f.closed_at or f.exit_fill_time,
                'entry_fill_time': f.entry_fill_time or f.opened_at,
                'exit_fill_time': f.exit_fill_time or f.closed_at,
            })

        return {
            'day': self.day,
            'date': self.day,
            'strategy': 'IRON_FLY_SPX',
            'status': 'open' if self.get_open_flies() else 'closed',
            'mode': self.mode,
            'em_open': self.em_open,
            'em_now': self.em_now,
            'ratio_vs_83': self.ratio_vs_83,
            'current_tier': self.current_tier,
            'first_fly_credit': self.first_fly_credit,
            'first_hour_high': self.first_hour_high,
            'first_hour_low': self.first_hour_low,
            'spot_trail': self.spot_trail[-60:],  # keep last 60 prints
            'open_risk': self.open_risk(),
            'realized_pnl': self.realized_pnl(),
            'unrealized_pnl': self.unrealized_pnl(),
            'total_fees': self.total_fees(),
            'net_pnl': self.net_pnl(),
            'flies': flies_dict,
            'structures': structures_list,
            'events': self.events,
        }

    def save_to_json(self, path: str) -> None:
        """Atomic write to JSON file with Windows retry and fallback."""
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'{os.path.basename(path)}.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass

            for attempt in range(10):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == 9:
                        try:
                            shutil.copyfile(tmp, path)
                            return
                        except Exception:
                            raise
                    time.sleep(0.05 * (attempt + 1))
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    @classmethod
    def load_from_json(cls, path: str, *, retries: int = 5, delay: float = 0.05) -> Optional[IronFlyBook]:
        if not os.path.isfile(path):
            return None
        data = None
        for attempt in range(retries):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                break
            except (PermissionError, json.JSONDecodeError):
                if attempt == retries - 1:
                    return None
                time.sleep(delay * (attempt + 1))
            except Exception as exc:
                log.warning('Failed to read IronFlyBook from %s: %s', path, exc)
                return None
        if not data:
            return None
        try:
            book = cls(
                day=data.get('day', ''),
                mode=data.get('mode', 'PAPER'),
                em_open=data.get('em_open'),
                em_now=data.get('em_now'),
                ratio_vs_83=data.get('ratio_vs_83'),
                current_tier=data.get('current_tier', 0),
                first_fly_credit=data.get('first_fly_credit'),
                first_hour_high=data.get('first_hour_high'),
                first_hour_low=data.get('first_hour_low'),
                spot_trail=data.get('spot_trail', []),
                events=data.get('events', []),
            )
            for fid, f_data in (data.get('flies') or {}).items():
                book.flies[fid] = IronFly.from_dict(f_data)
            return book
        except Exception as exc:
            log.warning('Failed to parse IronFlyBook from %s: %s', path, exc)
            return None
