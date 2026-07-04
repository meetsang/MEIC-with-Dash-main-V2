"""Shared MQTT price cache — single consumer path for streamer-published mids."""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

import paho.mqtt.client as mqtt

from common.broker_factory import get_mqtt_topic_prefix
from common.symbols import mqtt_topic_symbol, to_tastytrade
from streaming import config as stream_config

log = logging.getLogger(__name__)

_shared_cache: Optional['MqttPriceCache'] = None
_shared_lock = threading.Lock()


class MqttPriceCache:
    """Thread-safe latest-price cache fed by MQTT topics from publish_tastytrade.py."""

    def __init__(self, topic_prefix: Optional[str] = None):
        self._prefix = topic_prefix or get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
        self._prices: Dict[str, float] = {}
        self._overrides: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._client: Optional[mqtt.Client] = None
        self.kill_switch: bool = False

    def is_running(self) -> bool:
        return self._client is not None

    def start(self) -> None:
        if self._client is not None:
            return
        self._client = mqtt.Client()
        self._client.on_message = self._on_message
        self._client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
        kill_topic = self._prefix + 'MEIC_Close_All'
        self._client.subscribe(f'{self._prefix}#')
        self._client.subscribe(kill_topic)
        self._client.loop_start()
        # Allow broker to deliver retained mids before first read
        time.sleep(0.3)
        log.debug('MQTT price cache subscribed to %s#', self._prefix)

    def stop(self) -> None:
        if self._client:
            self._client.loop_stop()
            self._client.disconnect()
            self._client = None

    def _on_message(self, client, userdata, msg) -> None:
        try:
            topic = msg.topic
            if topic.endswith('MEIC_Close_All'):
                self.kill_switch = msg.payload.decode().strip().lower() == 'true'
                return
            if not topic.startswith(self._prefix):
                return
            symbol = topic[len(self._prefix):]
            price = float(msg.payload.decode())
            with self._lock:
                self._prices[symbol] = price
        except (ValueError, UnicodeDecodeError):
            pass

    def set_override(self, symbol: str, price: float) -> None:
        """Inject a synthetic mid (breach simulation tests only)."""
        key = to_tastytrade(symbol)
        with self._lock:
            self._overrides[key] = price

    def clear_override(self, symbol: str) -> None:
        """Remove one breach-simulation override."""
        keys = {
            symbol,
            to_tastytrade(symbol),
            mqtt_topic_symbol(symbol, 'tastytrade'),
            mqtt_topic_symbol(symbol, 'schwab'),
        }
        with self._lock:
            for k in keys:
                self._overrides.pop(k, None)

    def clear_overrides(self) -> None:
        with self._lock:
            self._overrides.clear()

    def _lookup_keys(self, symbol: str) -> List[str]:
        """Symbol aliases for MQTT cache lookup (never fall back to SPX index)."""
        return [
            symbol,
            to_tastytrade(symbol),
            mqtt_topic_symbol(symbol, 'tastytrade'),
            mqtt_topic_symbol(symbol, 'schwab'),
        ]

    def get(self, symbol: str) -> Optional[float]:
        """Look up price by any symbol format (includes breach-test overrides)."""
        keys = self._lookup_keys(symbol)
        with self._lock:
            for k in keys:
                if k in self._overrides:
                    return self._overrides[k]
            for k in keys:
                if k in self._prices:
                    return self._prices[k]
        return None

    def get_market_mid(self, symbol: str) -> Optional[float]:
        """Live MQTT mid only — ignores breach-simulation overrides."""
        keys = self._lookup_keys(symbol)
        with self._lock:
            for k in keys:
                if k in self._prices:
                    return self._prices[k]
        return None

    def get_spx(self) -> Optional[float]:
        return self.get('SPX')

    def wait_for(self, symbol: str, timeout: float = 10.0, poll: float = 0.2) -> Optional[float]:
        """Block until streamer publishes a mid for symbol (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            price = self.get(symbol)
            if price is not None:
                return price
            time.sleep(poll)
        return None


def get_shared_cache() -> MqttPriceCache:
    """Process-wide cache — same MQTT feed as stop_monitor."""
    global _shared_cache
    with _shared_lock:
        if _shared_cache is None:
            _shared_cache = MqttPriceCache()
        return _shared_cache


def ensure_cache_started() -> MqttPriceCache:
    cache = get_shared_cache()
    if not cache.is_running():
        cache.start()
    return cache


def register_symbols_and_wait(
    schwab_symbols: List[str],
    lot: str,
    logger,
    *,
    wait_seconds: Optional[float] = None,
) -> None:
    """Add symbols to optsymbols.json so streamer subscribes, then wait for MQTT."""
    import meic0dte.app.config as app_config
    from meic0dte.app import utilities as util

    unique = list(dict.fromkeys(schwab_symbols))
    if not util.update_options_symbols(unique, lot, logger):
        return
    delay = wait_seconds if wait_seconds is not None else app_config.STREAMER_QUOTE_WAIT
    if delay > 0:
        logger.info('Waiting %ss for streamer MQTT on %d symbols', delay, len(unique))
        time.sleep(delay)
