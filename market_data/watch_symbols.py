"""Map MQTT topic symbols to market_data watch symbols (indices/equities only)."""
from __future__ import annotations

from typing import Optional

from common.market_watch import (
    TRADE_SIZE_TOPIC_SUFFIX,
    VOLUME_TOPIC_SUFFIX,
    WATCH_SYMBOLS,
    canonical_watch_symbol,
    get_drma_stock_watch_symbols,
    mqtt_topic_from_dxlink,
)

_WATCH_SET = frozenset(WATCH_SYMBOLS)


def watch_symbol_from_mqtt_topic(topic_symbol: str) -> Optional[str]:
    """Return canonical watch symbol for an MQTT topic suffix, or None if not watched."""
    text = (topic_symbol or '').strip()
    if not text:
        return None
    # Strip optional broker prefix e.g. TASTYTRADE/ or SCHWAB/
    if '/' in text and not text.startswith(('/ES', '/MES', '/NQ', '/MNQ')):
        text = text.split('/')[-1]
    if text.endswith(VOLUME_TOPIC_SUFFIX):
        text = text[: -len(VOLUME_TOPIC_SUFFIX)]
    elif text.endswith(TRADE_SIZE_TOPIC_SUFFIX):
        text = text[: -len(TRADE_SIZE_TOPIC_SUFFIX)]
    if text in _WATCH_SET or text in get_drma_stock_watch_symbols():
        return text
    canonical = canonical_watch_symbol(text)
    if canonical:
        return canonical
    mqtt = mqtt_topic_from_dxlink(text)
    if mqtt in _WATCH_SET or mqtt in get_drma_stock_watch_symbols():
        return mqtt
    return None
