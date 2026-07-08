"""Trade entry/exit timestamps for dashboard grids."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional


def _iso_or_epoch_to_iso(ts: Any) -> str:
    if ts is None or ts == '':
        return ''
    if isinstance(ts, (int, float)):
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone().isoformat(
                timespec='seconds',
            )
        except (TypeError, ValueError, OSError):
            return ''
    text = str(ts).strip()
    if not text:
        return ''
    if text[0].isdigit() and 'T' not in text and ':' not in text:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc).astimezone().isoformat(
                timespec='seconds',
            )
        except (TypeError, ValueError, OSError):
            return ''
    return text


def trade_entry_time_iso(trade: Dict[str, Any]) -> str:
    """ISO timestamp for spread entry (fill or order placement)."""
    entry = trade.get('entry') or {}
    return _iso_or_epoch_to_iso(entry.get('timestamp') or trade.get('time_opened'))


def trade_exit_time_iso(trade: Dict[str, Any]) -> str:
    """ISO timestamp for spread exit — full close preferred, else short-leg close start."""
    close = trade.get('close') or {}
    ts = close.get('timestamp')
    if ts:
        return _iso_or_epoch_to_iso(ts)
    if trade.get('short_closed_at') is not None:
        return _iso_or_epoch_to_iso(trade.get('short_closed_at'))
    return ''
