"""TradeSlot cache + mtime-gated disk merge (V3 §6.1, §8.1)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from blocks.stop import state as state_mod


@dataclass
class TradeSlot:
    path: str
    state: Dict[str, Any]
    disk_mtime: float = 0.0
    dirty: bool = False
    exit_job_id: Optional[str] = None
    last_broker_sync: float = 0.0
    long_chase_scheduled_at: Optional[float] = None
    alert_order_ids: Set[str] = field(default_factory=set)
    legacy_monitor: Any = None  # optional StopMonitor adapter for V3-2a open-path

    @property
    def status(self) -> str:
        return str(self.state.get('status') or '')

    @property
    def close_only_mode(self) -> bool:
        return bool(self.state.get('close_only_mode'))

    @classmethod
    def from_path(cls, path: str) -> 'TradeSlot':
        st = state_mod.load_state(path)
        return cls.from_loaded(path, st)

    @classmethod
    def from_loaded(cls, path: str, state: Dict[str, Any]) -> 'TradeSlot':
        return cls(path=path, state=state, disk_mtime=_path_mtime(path))


def _path_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def merge_policy(memory: Dict[str, Any], disk: Dict[str, Any]) -> Dict[str, Any]:
    """Merge disk into memory — port _maybe_merge_disk_stop_state rules + V3 exit fields."""
    merged = dict(memory)
    for key in (
        'close_only_mode',
        'exit_handler',
        'exit_started_at',
        'exit_last_step',
        'exit_last_progress_at',
        'exit_attempt',
        'exit_stalled',
        'exit_error',
        'spread_close_order_id',
        'close_mechanism',
        'status',
    ):
        if key in disk:
            merged[key] = disk[key]

    disk_stop = disk.get('active_stop') or {}
    if not disk_stop.get('order_id'):
        return merged

    mem_stop = merged.get('active_stop') or {}
    disk_oid = str(disk_stop['order_id'])
    mem_oid = str(mem_stop.get('order_id') or '')
    disk_st = str(disk_stop.get('status', '')).lower()
    mem_st = str(mem_stop.get('status', '')).lower()
    working = ('working', 'live', 'contingent', 'received', 'open')

    adopt = False
    if disk_st in working and mem_st in ('cancelled', 'canceled', 'rejected', ''):
        adopt = True
    elif disk_oid != mem_oid and disk_st in working:
        adopt = True

    if adopt:
        merged['active_stop'] = dict(disk_stop)
        if disk.get('stop_quantity') is not None:
            merged['stop_quantity'] = disk['stop_quantity']
        if disk.get('designated_stop_price') is not None:
            merged['designated_stop_price'] = disk['designated_stop_price']
        if disk.get('stop_history'):
            merged['stop_history'] = disk['stop_history']
    return merged


def merge_disk_state(slot: TradeSlot) -> None:
    mtime = _path_mtime(slot.path)
    if mtime <= slot.disk_mtime and not slot.dirty:
        return
    disk = state_mod.load_state(slot.path)
    slot.state = merge_policy(slot.state, disk)
    slot.disk_mtime = mtime
    slot.dirty = False
    if slot.legacy_monitor is not None:
        slot.legacy_monitor.state = slot.state


def save_slot(slot: TradeSlot) -> None:
    state_mod.save_state(slot.path, slot.state)
    slot.disk_mtime = _path_mtime(slot.path)
    slot.dirty = False
    if slot.legacy_monitor is not None:
        slot.legacy_monitor.state = slot.state
