"""State book, paired flies management, and atomic persistence for XSP Iron Fly."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from brokers.base import ComboLeg
from common import trades_layout
from strategies.iron_fly.book import DiaryEvent, FlyStatus, IronFly

log = logging.getLogger(__name__)


@dataclass
class IronFlyXspPair:
    """
    A paired XSP 2-fly structure corresponding to 1 scaled SPX fly.
    If SPX center ends in 5 (e.g. 7755 -> 775.5):
        fly_a has center 775 (qty 1)
        fly_b has center 776 (qty 1)
    If SPX center ends in 0 (e.g. 7750 -> 775.0):
        fly_a has center 775 (qty 1)
        fly_b has center 775 (qty 1)
    """
    pair_id: str
    spx_center: int
    center_ideal: float
    fly_a: IronFly
    fly_b: IronFly
    status: str = FlyStatus.OPENING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    opened_at: Optional[str] = None
    closed_at: Optional[str] = None

    @property
    def credit_total(self) -> float:
        return round((self.fly_a.credit or 0.0) + (self.fly_b.credit or 0.0), 2)

    @property
    def credit_avg(self) -> float:
        return round(self.credit_total / 2.0, 2)

    @property
    def credit(self) -> float:
        return self.credit_avg

    @property
    def be_low(self) -> float:
        return round(self.center_ideal - self.credit_avg, 2)

    @property
    def be_high(self) -> float:
        return round(self.center_ideal + self.credit_avg, 2)

    @property
    def unrealized_pnl(self) -> float:
        return round(self.fly_a.unrealized_pnl + self.fly_b.unrealized_pnl, 2)

    @property
    def open_pnl(self) -> float:
        return self.unrealized_pnl

    @property
    def realized_pnl(self) -> float:
        return round(self.fly_a.realized_pnl + self.fly_b.realized_pnl, 2)

    @property
    def fees_paid(self) -> float:
        return round(self.fly_a.fees_paid + self.fly_b.fees_paid, 2)

    @property
    def open_risk(self) -> float:
        if self.status not in (FlyStatus.OPEN, FlyStatus.CLOSING, FlyStatus.OPENING):
            return 0.0
        return round(self.fly_a.open_risk + self.fly_b.open_risk, 2)

    def to_dict(self) -> Dict[str, Any]:
        opened_ts = (
            self.opened_at
            or getattr(self.fly_a, 'opened_at', None)
            or getattr(self.fly_a, 'entry_fill_time', None)
            or self.created_at
        )
        closed_ts = (
            self.closed_at
            or getattr(self.fly_a, 'closed_at', None)
            or getattr(self.fly_a, 'exit_fill_time', None)
            or getattr(self.fly_b, 'closed_at', None)
            or getattr(self.fly_b, 'exit_fill_time', None)
        )
        return {
            'id': self.pair_id,
            'pair_id': self.pair_id,
            'spx_center': self.spx_center,
            'center_ideal': self.center_ideal,
            'center_a': self.fly_a.center,
            'center_b': self.fly_b.center,
            'fly_a': self.fly_a.to_dict(),
            'fly_b': self.fly_b.to_dict(),
            'status': self.status,
            'credit_total': self.credit_total,
            'credit_avg': self.credit_avg,
            'be_low': self.be_low,
            'be_high': self.be_high,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl': self.realized_pnl,
            'fees_paid': self.fees_paid,
            'open_risk': self.open_risk,
            'created_at': self.created_at,
            'opened_at': opened_ts,
            'closed_at': closed_ts,
            'entry_fill_time': opened_ts,
            'exit_fill_time': closed_ts,
        }


class IronFlyXspBook:
    """Manages the full active and historical lifecycle of XSP Iron Flies."""

    def __init__(self, date_str: str, root: Optional[str] = None):
        self.date = date_str
        self.root = root or trades_layout.project_root()
        self.pairs: Dict[str, IronFlyXspPair] = {}
        self.flies: Dict[str, IronFly] = {}
        self.pending_approval: Dict[str, IronFlyXspPair] = {}
        self.events: List[DiaryEvent] = []
        self.current_tier: int = 0
        self.em_open: Optional[float] = None
        self.em_now: Optional[float] = None
        self.ratio_vs_83: Optional[float] = None
        self.spot_trail: List[Tuple[str, float]] = []
        self.pairs_opened_today: int = 0
        self._override_open_risk: Optional[float] = None

    @property
    def file_path(self) -> str:
        active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, self.root)
        os.makedirs(active_dir, exist_ok=True)
        return os.path.join(active_dir, f'iron_fly_xsp_{self.date}.json')

    @property
    def history_file_path(self) -> str:
        hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, self.root)
        os.makedirs(hist_dir, exist_ok=True)
        return os.path.join(hist_dir, f'iron_fly_xsp_{self.date}.json')

    def add_pair(self, pair: IronFlyXspPair) -> None:
        self.pairs[pair.pair_id] = pair
        self.flies[pair.fly_a.id] = pair.fly_a
        self.flies[pair.fly_b.id] = pair.fly_b
        self.pairs_opened_today += 1
        self.log_event('ENTER_PAIR', f'Registered XSP pair {pair.pair_id}: {pair.center_ideal} ({pair.fly_a.center}/{pair.fly_b.center})')

    def get_short_legs_map(self) -> Set[int]:
        shorts: Set[int] = set()
        for f in self.flies.values():
            if f.status in (FlyStatus.OPEN, FlyStatus.OPENING, FlyStatus.CLOSING):
                shorts.add(f.center)
        return shorts

    def get_long_legs_map(self) -> Set[int]:
        longs: Set[int] = set()
        for f in self.flies.values():
            if f.status in (FlyStatus.OPEN, FlyStatus.OPENING, FlyStatus.CLOSING):
                longs.add(f.call_long)
                longs.add(f.put_long)
        return longs

    @property
    def open_risk(self) -> float:
        if self._override_open_risk is not None:
            return self._override_open_risk
        return sum(p.open_risk for p in self.pairs.values())

    @open_risk.setter
    def open_risk(self, val: float) -> None:
        self._override_open_risk = val

    @property
    def realized_pnl(self) -> float:
        return sum(p.realized_pnl for p in self.pairs.values())

    @property
    def open_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.pairs.values() if p.status == FlyStatus.OPEN)

    @property
    def total_fees(self) -> float:
        return sum(p.fees_paid for p in self.pairs.values())

    def log_event(self, event_type: str, msg: str) -> None:
        event = DiaryEvent(
            ts=datetime.now().isoformat(),
            type=event_type,
            msg=msg,
        )
        self.events.append(event)
        log.info('[XSP_DIARY %s] %s', event_type, msg)

    def record_spot(self, spot: float) -> None:
        self.spot_trail.append((datetime.now().isoformat(), round(spot, 2)))
        if len(self.spot_trail) > 100:
            self.spot_trail.pop(0)

    def save(self) -> None:
        data = self.to_dict()
        directory = os.path.dirname(self.file_path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'{os.path.basename(self.file_path)}.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            for attempt in range(10):
                try:
                    os.replace(tmp, self.file_path)
                    return
                except PermissionError:
                    if attempt == 9:
                        try:
                            shutil.copyfile(tmp, self.file_path)
                            return
                        except Exception:
                            raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            log.error('Could not save XSP Iron Fly book to %s: %s', self.file_path, exc)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def archive_to_history(self) -> None:
        self.save()
        hist_path = self.history_file_path
        directory = os.path.dirname(hist_path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=f'{os.path.basename(hist_path)}.', suffix='.tmp')
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
                    os.replace(tmp, hist_path)
                    log.info('Archived XSP Iron Fly book to %s', hist_path)
                    return
                except PermissionError:
                    if attempt == 9:
                        try:
                            shutil.copyfile(tmp, hist_path)
                            log.info('Archived XSP Iron Fly book to %s via copy', hist_path)
                            return
                        except Exception:
                            raise
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            log.error('Could not archive XSP Iron Fly book to %s: %s', hist_path, exc)
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def to_dict(self) -> Dict[str, Any]:
        return {
            'date': self.date,
            'underlying': 'XSP',
            'current_tier': self.current_tier,
            'em_open': self.em_open,
            'em_now': self.em_now,
            'ratio_vs_83': self.ratio_vs_83,
            'spot_trail': self.spot_trail[-20:],
            'events': [e.to_dict() for e in self.events],
            'pending_approval': [p.to_dict() for p in self.pending_approval.values()],
            'pairs': {pid: p.to_dict() for pid, p in self.pairs.items()},
            'flies': {fid: f.to_dict() for fid, f in self.flies.items()},
            # Structures list for manual_close.py compatibility
            'structures': [f.to_dict() for f in self.flies.values()],
            'realized_pnl': round(self.realized_pnl, 2),
            'open_pnl': round(self.open_pnl, 2),
            'total_fees': round(self.total_fees, 2),
            'open_risk': round(self.open_risk, 2),
            'pairs_opened_today': self.pairs_opened_today,
            'flies_opened_today': self.pairs_opened_today * 2,
        }

    @classmethod
    def load_or_create(cls, date_str: str, root: Optional[str] = None) -> 'IronFlyXspBook':
        book = cls(date_str, root)
        if os.path.isfile(book.file_path):
            try:
                with open(book.file_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                book.current_tier = data.get('current_tier', 0)
                book.em_open = data.get('em_open')
                book.em_now = data.get('em_now')
                book.ratio_vs_83 = data.get('ratio_vs_83')
                book.pairs_opened_today = data.get('pairs_opened_today', 0)
                book.events = [DiaryEvent(**e) for e in data.get('events', [])]

                for pdata in data.get('pairs', {}).values():
                    fa_data = pdata.get('fly_a', {})
                    fb_data = pdata.get('fly_b', {})
                    fa_legs = [ComboLeg(**l) for l in fa_data.pop('legs', [])]
                    fb_legs = [ComboLeg(**l) for l in fb_data.pop('legs', [])]
                    fly_a = IronFly(**fa_data, legs=fa_legs)
                    fly_b = IronFly(**fb_data, legs=fb_legs)
                    pair = IronFlyXspPair(
                        pair_id=pdata['pair_id'],
                        spx_center=pdata.get('spx_center', int(pdata['center_ideal'] * 10)),
                        center_ideal=pdata['center_ideal'],
                        fly_a=fly_a,
                        fly_b=fly_b,
                        status=pdata.get('status', FlyStatus.OPEN),
                        created_at=pdata.get('created_at', ''),
                    )
                    book.pairs[pair.pair_id] = pair
                    book.flies[fly_a.id] = fly_a
                    book.flies[fly_b.id] = fly_b
            except Exception as exc:
                log.warning('Could not load XSP Iron Fly book from %s: %s — starting fresh', book.file_path, exc)
        return book
