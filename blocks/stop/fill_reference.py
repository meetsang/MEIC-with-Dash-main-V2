"""Fill reference time for post-fill software-breach gating."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from blocks.stop import state as state_mod

log = logging.getLogger(__name__)

SOURCE_BROKER_FILLED_AT = 'broker_filled_at'
SOURCE_FILL_SYNC_RESOLVED = 'fill_sync_resolved'
SOURCE_OPEN_ORDER_SYNC = 'open_order_last_sync'
SOURCE_ENTRY_TIMESTAMP = 'entry_timestamp'


def _parse_iso_epoch(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return ts if ts > 0 else None
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return None


def resolve_fill_reference_epoch(state: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    """Return persisted or derived fill reference without mutating state."""
    lifecycle = state.get('lifecycle') or {}
    stored = lifecycle.get('fill_reference_epoch')
    if stored is not None:
        try:
            return float(stored), lifecycle.get('fill_reference_source')
        except (TypeError, ValueError):
            pass

    open_order = state.get('open_order') or {}
    fill_sync = open_order.get('fill_sync') or {}
    resolved = fill_sync.get('resolved_at_epoch')
    if resolved is not None:
        try:
            return float(resolved), SOURCE_FILL_SYNC_RESOLVED
        except (TypeError, ValueError):
            pass

    last_sync = open_order.get('last_sync_epoch')
    if last_sync is not None:
        try:
            return float(last_sync), SOURCE_OPEN_ORDER_SYNC
        except (TypeError, ValueError):
            pass

    entry = state_mod.section(state, 'entry')
    entry_ts = _parse_iso_epoch(entry.get('timestamp'))
    if entry_ts is not None:
        return entry_ts, SOURCE_ENTRY_TIMESTAMP

    return None, SOURCE_ENTRY_TIMESTAMP


def ensure_fill_reference_epoch(
    state: Dict[str, Any],
    *,
    broker_filled_at: Optional[float] = None,
) -> Optional[float]:
    """Persist fill_reference_epoch using resolution order from the tech spec."""
    lifecycle = state_mod.section(state, 'lifecycle')
    existing = lifecycle.get('fill_reference_epoch')
    if existing is not None:
        try:
            return float(existing)
        except (TypeError, ValueError):
            pass

    epoch: Optional[float] = None
    source: Optional[str] = None

    if broker_filled_at is not None:
        try:
            ts = float(broker_filled_at)
            if ts > 1e12:
                ts /= 1000.0
            if ts > 0:
                epoch, source = ts, SOURCE_BROKER_FILLED_AT
        except (TypeError, ValueError):
            pass

    if epoch is None:
        fill_sync = (state.get('open_order') or {}).get('fill_sync') or {}
        resolved = fill_sync.get('resolved_at_epoch')
        if resolved is not None:
            try:
                epoch, source = float(resolved), SOURCE_FILL_SYNC_RESOLVED
            except (TypeError, ValueError):
                pass

    if epoch is None:
        open_order = state.get('open_order') or {}
        last_sync = open_order.get('last_sync_epoch')
        if last_sync is not None:
            try:
                epoch, source = float(last_sync), SOURCE_OPEN_ORDER_SYNC
            except (TypeError, ValueError):
                pass

    if epoch is None:
        entry = state_mod.section(state, 'entry')
        entry_ts = _parse_iso_epoch(entry.get('timestamp'))
        if entry_ts is not None:
            epoch, source = entry_ts, SOURCE_ENTRY_TIMESTAMP

    if epoch is None:
        return None

    lifecycle['fill_reference_epoch'] = round(float(epoch), 6)
    lifecycle['fill_reference_source'] = source
    return float(epoch)
