"""Load/save daily session CSV plans (MEIC + Manual)."""
from __future__ import annotations

import csv
import glob
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime, time as dt_time
from typing import Any, Dict, List, Optional

from common import trades_layout
from common.session_cleanup import central_today

MEIC_CSV_COLUMNS = [
    'slot_key',
    'lot',
    'side',
    'entry_window_start',
    'entry_window_end',
    'entry_condition',
    'paused',
    'skip',
    'quantity',
    'stop_mode',
    'stop_multiplier',
    'stop_percent',
    'width',
    'credit_min',
    'credit_max',
    'chase1_mode',
    'chase1_max',
    'chase2_mode',
    'chase2_max',
    'fill_wait_sec',
    'max_attempts',
    'state',
    'trade_path',
    'short_strike',
    'long_strike',
    'limit_credit',
    'on_unfilled',
    'expiry',
    'parent_trade_path',
]


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def session_dir(root: Optional[str] = None) -> str:
    return os.path.join(trades_layout.trades_root(root or _project_root()), 'session')


def meic_session_path(for_date: Optional[date] = None, root: Optional[str] = None) -> str:
    d = for_date or central_today()
    return os.path.join(session_dir(root), f'{trades_layout.STRATEGY_MEIC}_{d.isoformat()}.csv')


def parse_width(width: str) -> tuple[int, int]:
    """Parse '25-35' or '25' into (min, max)."""
    text = (width or '25-35').strip()
    if '-' in text:
        a, b = text.split('-', 1)
        return int(a.strip()), int(b.strip())
    w = int(text)
    return w, w


def parse_time_field(value: str) -> dt_time:
    parts = value.strip().split(':')
    if len(parts) == 2:
        return dt_time(int(parts[0]), int(parts[1]))
    raise ValueError(f'invalid time field: {value!r}')


def format_time_field(t: dt_time) -> str:
    return f'{t.hour:02d}:{t.minute:02d}'


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'y')


def _format_bool(value: bool) -> str:
    return 'true' if value else 'false'


@dataclass
class SessionRow:
    slot_key: str
    lot: str
    side: str
    entry_window_start: str
    entry_window_end: str
    entry_condition: str = 'time'
    paused: bool = False
    skip: bool = False
    quantity: int = 1
    stop_mode: str = 'multiplier'
    stop_multiplier: float = 2.0
    stop_percent: str = ''
    width: str = '25-35'
    credit_min: float = 0.90
    credit_max: float = 1.85
    chase1_mode: str = 'chase_same_trade'
    chase1_max: int = 3
    chase2_mode: str = 'build_new_strikes'
    chase2_max: int = 7
    fill_wait_sec: int = 5
    max_attempts: int = 10
    state: str = 'pending'
    trade_path: str = ''
    short_strike: int = 0
    long_strike: int = 0
    limit_credit: float = 0.0
    on_unfilled: str = ''
    expiry: str = ''
    parent_trade_path: str = ''

    @property
    def is_manual(self) -> bool:
        return (self.entry_condition or '').lower() == 'manual'

    @classmethod
    def from_csv_dict(cls, row: Dict[str, str]) -> 'SessionRow':
        data: Dict[str, Any] = {}
        int_fields = {
            'quantity', 'chase1_max', 'chase2_max',
            'fill_wait_sec', 'max_attempts', 'short_strike', 'long_strike',
        }
        float_fields = {'credit_min', 'credit_max', 'limit_credit', 'stop_multiplier'}
        for f in fields(cls):
            raw = row.get(f.name, '')
            if f.name in ('paused', 'skip'):
                data[f.name] = _parse_bool(raw)
            elif f.name in int_fields:
                data[f.name] = int(raw) if str(raw).strip() else 0 if f.name in ('short_strike', 'long_strike') else getattr(cls, f.name)
            elif f.name in float_fields:
                data[f.name] = float(raw) if str(raw).strip() else 0.0 if f.name == 'limit_credit' else getattr(cls, f.name)
            else:
                data[f.name] = raw if raw is not None else getattr(cls, f.name)
        return cls(**data)

    def to_csv_dict(self) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for f in fields(self):
            val = getattr(self, f.name)
            if f.name in ('paused', 'skip'):
                out[f.name] = _format_bool(bool(val))
            elif f.name in ('short_strike', 'long_strike'):
                out[f.name] = '' if not val else str(int(val))
            elif f.name == 'limit_credit':
                out[f.name] = '' if not val else f'{float(val):.2f}'
            elif f.name in ('credit_min', 'credit_max'):
                out[f.name] = f'{float(val):.2f}'
            elif f.name == 'stop_multiplier':
                out[f.name] = '' if not val else f'{float(val):g}'
            else:
                out[f.name] = '' if val is None else str(val)
        return out

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['entry_window_start'] = self.entry_window_start
        d['entry_window_end'] = self.entry_window_end
        return d

    def window_start_time(self) -> dt_time:
        return parse_time_field(self.entry_window_start)

    def window_end_time(self) -> dt_time:
        return parse_time_field(self.entry_window_end)

    def is_in_window(self, now_time: dt_time) -> bool:
        if self.is_manual:
            return True
        if not str(self.entry_window_start or '').strip():
            return True
        return self.window_start_time() <= now_time <= self.window_end_time()


class SessionPlan:
    def __init__(self, path: str, strategy: str, rows: List[SessionRow]):
        self.path = path
        self.strategy = strategy
        self.rows = rows

    @classmethod
    def load(cls, path: str, *, strategy: str = trades_layout.STRATEGY_MEIC) -> 'SessionPlan':
        rows: List[SessionRow] = []
        with open(path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.DictReader(f)
            for raw in reader:
                rows.append(SessionRow.from_csv_dict(raw))
        return cls(path, strategy, rows)

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=MEIC_CSV_COLUMNS, lineterminator='\n')
                writer.writeheader()
                for row in self.rows:
                    writer.writerow(row.to_csv_dict())
            for attempt in range(8):
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def row_by_slot_key(self, slot_key: str) -> Optional[SessionRow]:
        for row in self.rows:
            if row.slot_key == slot_key:
                return row
        return None

    def update_row(self, slot_key: str, **fields: Any) -> None:
        row = self.row_by_slot_key(slot_key)
        if row is None:
            raise KeyError(f'unknown slot_key: {slot_key}')
        for key, val in fields.items():
            if not hasattr(row, key):
                raise ValueError(f'unknown field: {key}')
            if key in ('paused', 'skip') and isinstance(val, str):
                val = _parse_bool(val)
            setattr(row, key, val)

    def append_row(self, row: SessionRow) -> None:
        if self.row_by_slot_key(row.slot_key):
            raise ValueError(f'duplicate slot_key: {row.slot_key}')
        self.rows.append(row)


def manual_session_path(for_date: Optional[date] = None, root: Optional[str] = None) -> str:
    d = for_date or central_today()
    return os.path.join(session_dir(root), f'{trades_layout.STRATEGY_MANUAL}_{d.isoformat()}.csv')


def ensure_manual_session(root: Optional[str] = None) -> str:
    """Create empty manual session CSV with header if missing."""
    path = manual_session_path(root=root)
    if os.path.isfile(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plan = SessionPlan(path, trades_layout.STRATEGY_MANUAL, [])
    plan.save()
    return path


def load_manual_session_today(root: Optional[str] = None) -> Optional[SessionPlan]:
    path = manual_session_path(root=root)
    if not os.path.isfile(path):
        return None
    return SessionPlan.load(path, strategy=trades_layout.STRATEGY_MANUAL)


def load_meic_session_today(root: Optional[str] = None) -> Optional[SessionPlan]:
    path = meic_session_path(root=root)
    if not os.path.isfile(path):
        return None
    return SessionPlan.load(path, strategy=trades_layout.STRATEGY_MEIC)


def glob_session_plans_today(root: Optional[str] = None) -> List[str]:
    d = central_today().isoformat()
    pattern = os.path.join(session_dir(root), f'*_{d}.csv')
    return sorted(glob.glob(pattern))
