"""MQTT price reader for market data recorder."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, Optional

from common.broker_factory import get_mqtt_topic_prefix
from common.mqtt_prices import MqttPriceCache
from market_data import config

log = logging.getLogger(__name__)


class MqttQuoteReader:
    """Poll shared MQTT cache; only forward prices that changed since last poll."""

    def __init__(self, symbols=None):
        self.symbols = tuple(symbols or config.WATCH_SYMBOLS)
        prefix = get_mqtt_topic_prefix() or 'TASTYTRADE/'
        self._cache = MqttPriceCache(topic_prefix=prefix)
        self._last_seen: Dict[str, float] = {}
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self._cache.is_running():
            self._cache.start()

    def stop(self) -> None:
        self._cache.stop()

    def poll_changed(self) -> Dict[str, float]:
        """Return {symbol: price} for symbols with new mids since last poll."""
        out: Dict[str, float] = {}
        with self._lock:
            for sym in self.symbols:
                price = self._cache.get_market_mid(sym)
                if price is None:
                    price = self._cache.get(sym)
                if price is None:
                    continue
                price = float(price)
                if sym in self._last_seen and self._last_seen[sym] == price:
                    continue
                self._last_seen[sym] = price
                out[sym] = price
        return out

    def poll_latest(self) -> Dict[str, float]:
        """Return latest mid for every symbol that has MQTT data."""
        out: Dict[str, float] = {}
        with self._lock:
            for sym in self.symbols:
                price = self._cache.get_market_mid(sym)
                if price is None:
                    price = self._cache.get(sym)
                if price is None:
                    continue
                price = float(price)
                self._last_seen[sym] = price
                out[sym] = price
        return out

    @property
    def cache(self) -> MqttPriceCache:
        return self._cache

    def wait_for_any(self, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.poll_latest():
                return True
            time.sleep(2.0)
        return False
