"""Detect shared active_stop.order_id across open tranches (live-safety guard)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

from blocks.stop import state as state_mod

log = logging.getLogger(__name__)

_TERMINAL_STOP_STATUSES = frozenset(
    ('filled', 'cancelled', 'canceled', 'rejected', 'expired', 'unknown', '')
)


@dataclass(frozen=True)
class DuplicateStopOwnership:
    order_id: str
    paths: tuple[str, ...]


def _active_stop_order_id(state: dict) -> str:
    active = state.get('active_stop') or {}
    if not isinstance(active, dict):
        return ''
    if str(active.get('status') or '').lower() in _TERMINAL_STOP_STATUSES:
        return ''
    return str(active.get('order_id') or '')


def collect_claimed_stop_order_ids(
    paths: Optional[Iterable[str]] = None,
) -> Dict[str, str]:
    """Map broker stop order_id → JSON path for open/closing trades."""
    claimed: Dict[str, str] = {}
    for path in paths or state_mod.iter_active_trade_paths():
        try:
            st = state_mod.load_state(path)
        except OSError:
            continue
        if str(st.get('status') or '') not in ('open', 'closing'):
            continue
        oid = _active_stop_order_id(st)
        if oid:
            claimed[oid] = path
    return claimed


def scan_duplicate_stop_ownership(
    paths: Optional[Iterable[str]] = None,
) -> List[DuplicateStopOwnership]:
    """Return order IDs referenced by more than one open/closing JSON."""
    by_oid: Dict[str, List[str]] = {}
    for path in paths or state_mod.iter_active_trade_paths():
        try:
            st = state_mod.load_state(path)
        except OSError:
            continue
        if str(st.get('status') or '') not in ('open', 'closing'):
            continue
        oid = _active_stop_order_id(st)
        if not oid:
            continue
        by_oid.setdefault(oid, []).append(path)

    duplicates: List[DuplicateStopOwnership] = []
    for oid, refs in sorted(by_oid.items()):
        if len(refs) > 1:
            duplicates.append(DuplicateStopOwnership(order_id=oid, paths=tuple(sorted(refs))))
    return duplicates


def ownership_conflict_paths(
    duplicates: Optional[List[DuplicateStopOwnership]] = None,
) -> Set[str]:
    """Paths that must not treat active_stop as exclusive protection."""
    dups = duplicates if duplicates is not None else scan_duplicate_stop_ownership()
    conflict: Set[str] = set()
    for item in dups:
        conflict.update(item.paths)
    return conflict


def apply_ownership_conflict_flags(
    duplicates: List[DuplicateStopOwnership],
    *,
    clear_when_resolved: bool = True,
) -> None:
    """Set lifecycle.stop_ownership_conflict on involved JSONs; clear when resolved."""
    conflict_paths = ownership_conflict_paths(duplicates)
    all_paths = list(state_mod.iter_active_trade_paths())
    for path in all_paths:
        try:
            st = state_mod.load_state(path)
        except OSError:
            continue
        if str(st.get('status') or '') not in ('open', 'closing'):
            continue
        lc = st.setdefault('lifecycle', {})
        if not isinstance(lc, dict):
            lc = {}
            st['lifecycle'] = lc
        in_conflict = path in conflict_paths
        if in_conflict:
            lc['stop_ownership_conflict'] = True
            state_mod.save_state(path, st)
        elif clear_when_resolved and lc.pop('stop_ownership_conflict', None):
            state_mod.save_state(path, st)

    for dup in duplicates:
        log.critical(
            'CRITICAL duplicate active_stop ownership order_id=%s paths=%s',
            dup.order_id,
            list(dup.paths),
        )


def has_ownership_conflict(state: dict) -> bool:
    lc = state.get('lifecycle') or {}
    return bool(isinstance(lc, dict) and lc.get('stop_ownership_conflict'))
