"""Classify terminal broker errors for expired / halted option symbols."""
from __future__ import annotations

_STOPPED_TRADING_MARKERS = (
    'instruments_stopped_trading',
    'stopped trading',
    'Stopped symbols',
)


def is_instruments_stopped_trading_error(message: str) -> bool:
    text = (message or '').lower()
    return any(marker.lower() in text for marker in _STOPPED_TRADING_MARKERS)
