"""Map MQTT topic symbols to market_data watch symbols (indices/equities only)."""
from __future__ import annotations

from typing import Optional

from market_data import config

_TOPIC_ALIASES = {
    'SPX': 'SPX',
    '$SPX': 'SPX',
    '.$SPX': 'SPX',
    'VIX': 'VIX',
    '$VIX': 'VIX',
    '.$VIX': 'VIX',
    'VXN': 'VXN',
    '$VXN': 'VXN',
    '.$VXN': 'VXN',
    'QQQ': 'QQQ',
    'IWM': 'IWM',
}

_WATCH_SET = frozenset(config.WATCH_SYMBOLS)


def watch_symbol_from_mqtt_topic(topic_symbol: str) -> Optional[str]:
    """Return canonical watch symbol for an MQTT topic suffix, or None if not watched."""
    text = (topic_symbol or '').strip()
    if not text:
        return None
    if text in _WATCH_SET:
        return text
    return _TOPIC_ALIASES.get(text)
