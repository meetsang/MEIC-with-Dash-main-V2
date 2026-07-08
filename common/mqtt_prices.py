"""Shared MQTT price cache — single consumer path for streamer-published mids."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

TickListener = Callable[[str, float, float], None]

import paho.mqtt.client as mqtt

from common.broker_factory import get_mqtt_topic_prefix
from common.symbols import mqtt_topic_symbol, to_tastytrade
from common.trades_layout import ops_path
from streaming import config as stream_config

log = logging.getLogger(__name__)

MQTT_CACHE_HEALTH_FILE = 'mqtt_cache_health.json'
DEFAULT_STALE_THRESHOLD_SEC = 30.0
STALE_WARN_SEC = 60.0
RECONNECT_BASE_SEC = 1.0
RECONNECT_MAX_SEC = 60.0

_shared_cache: Optional['MqttPriceCache'] = None
_shared_lock = threading.Lock()


class MqttPriceCache:
    """Thread-safe latest-price cache fed by MQTT topics from publish_tastytrade.py."""

    def __init__(
        self,
        topic_prefix: Optional[str] = None,
        *,
        stale_threshold_sec: float = DEFAULT_STALE_THRESHOLD_SEC,
    ):
        self._prefix = topic_prefix or get_mqtt_topic_prefix() or stream_config.TOPIC_PREFIX
        self._stale_threshold_sec = float(stale_threshold_sec)
        self._prices: Dict[str, float] = {}
        self._overrides: Dict[str, float] = {}
        self._last_symbol_at: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._client: Optional[mqtt.Client] = None
        self._start_lock = threading.Lock()
        self._reconnect_timer: Optional[threading.Timer] = None
        self._reconnect_attempts = 0
        self._connected = False
        self._last_msg_at: float = 0.0
        self._last_error: Optional[str] = None
        self._last_stale_warn_at: float = 0.0
        self._tick_listeners: List[TickListener] = []
        self._listener_lock = threading.Lock()
        self.kill_switch: bool = False

    def is_running(self) -> bool:
        return self._client is not None

    def is_stale(self) -> bool:
        with self._lock:
            return self._is_stale_locked()

    def _is_stale_locked(self) -> bool:
        if not self.is_running():
            return False
        if self._last_msg_at <= 0:
            return True
        age = time.time() - self._last_msg_at
        if age > STALE_WARN_SEC:
            self._maybe_log_stale_warning_locked(age)
        return age > self._stale_threshold_sec

    def _maybe_log_stale_warning_locked(self, age: float) -> None:
        now = time.time()
        if now - self._last_stale_warn_at < STALE_WARN_SEC:
            return
        self._last_stale_warn_at = now
        log.warning(
            'MQTT price cache stale %.0fs (threshold %.0fs)',
            age,
            self._stale_threshold_sec,
        )

    def start(self) -> None:
        with self._start_lock:
            if self._client is not None:
                return
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
            client.on_connect = self._on_connect
            client.on_disconnect = self._on_disconnect
            client.on_message = self._on_message
            try:
                client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
                client.loop_start()
            except Exception as exc:
                self._last_error = str(exc)
                log.error('MQTT price cache start failed: %s', exc)
                raise
            self._client = client
            time.sleep(0.3)
            log.info('MQTT price cache started for %s#', self._prefix)

    def stop(self) -> None:
        with self._start_lock:
            timer = self._reconnect_timer
            self._reconnect_timer = None
            if timer is not None:
                timer.cancel()
            client = self._client
            self._client = None
            self._connected = False
        if client:
            client.loop_stop()
            client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            self._last_error = f'connect rc={reason_code}'
            log.warning('MQTT price cache connect failed: %s', self._last_error)
            return
        self._connected = True
        self._reconnect_attempts = 0
        self._last_error = None
        kill_topic = self._prefix + 'MEIC_Close_All'
        client.subscribe(f'{self._prefix}#')
        client.subscribe(kill_topic)
        log.info('MQTT price cache connected; subscribed %s#', self._prefix)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected = False
        self._last_error = f'disconnect rc={reason_code}'
        log.warning('MQTT price cache disconnected: %s', self._last_error)
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._client is None:
            return
        with self._start_lock:
            if self._reconnect_timer is not None:
                return
            delay = min(
                RECONNECT_MAX_SEC,
                RECONNECT_BASE_SEC * (2 ** min(self._reconnect_attempts, 6)),
            )
            self._reconnect_attempts += 1
            attempt = self._reconnect_attempts
            timer = threading.Timer(delay, self._do_reconnect, args=(attempt,))
            timer.daemon = True
            self._reconnect_timer = timer
            timer.start()

    def _do_reconnect(self, attempt: int) -> None:
        with self._start_lock:
            self._reconnect_timer = None
            client = self._client
        if client is None:
            return
        try:
            log.info('MQTT price cache reconnecting (attempt %d)', attempt)
            client.reconnect()
        except Exception as exc:
            self._last_error = str(exc)
            log.warning('MQTT price cache reconnect failed: %s', exc)
            self._schedule_reconnect()

    def add_tick_listener(self, listener: TickListener) -> None:
        """Register callback(symbol, price, epoch_sec) on each MQTT mid update."""
        with self._listener_lock:
            if listener not in self._tick_listeners:
                self._tick_listeners.append(listener)

    def remove_tick_listener(self, listener: TickListener) -> None:
        with self._listener_lock:
            try:
                self._tick_listeners.remove(listener)
            except ValueError:
                pass

    def _notify_tick_listeners(self, symbol: str, price: float, epoch: float) -> None:
        with self._listener_lock:
            listeners = list(self._tick_listeners)
        for listener in listeners:
            try:
                listener(symbol, price, epoch)
            except Exception:
                log.exception('MQTT tick listener failed for %s', symbol)

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
            now = time.time()
            with self._lock:
                self._prices[symbol] = price
                self._last_symbol_at[symbol] = now
                self._last_msg_at = now
            self._notify_tick_listeners(symbol, price, now)
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
            if self._is_stale_locked():
                return None
            for k in keys:
                if k in self._prices:
                    return self._prices[k]
        return None

    def get_market_mid(self, symbol: str) -> Optional[float]:
        """Live MQTT mid only — ignores breach-simulation overrides."""
        keys = self._lookup_keys(symbol)
        with self._lock:
            if self._is_stale_locked():
                return None
            for k in keys:
                if k in self._prices:
                    return self._prices[k]
        return None

    def get_spx(self) -> Optional[float]:
        return self.get('SPX')

    def cache_health(self) -> Dict[str, Any]:
        with self._lock:
            age = (
                None
                if self._last_msg_at <= 0
                else max(0.0, time.time() - self._last_msg_at)
            )
            stale = self._is_stale_locked() if self.is_running() else False
            last_msg_iso = None
            if self._last_msg_at > 0:
                last_msg_iso = (
                    datetime.fromtimestamp(self._last_msg_at, tz=timezone.utc)
                    .astimezone()
                    .isoformat(timespec='seconds')
                )
            return {
                'ts': datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds'),
                'connected': self._connected,
                'running': self.is_running(),
                'last_msg_at': last_msg_iso,
                'age_seconds': age,
                'stale': stale,
                'prefix': self._prefix,
                'topics': [f'{self._prefix}#', f'{self._prefix}MEIC_Close_All'],
                'price_count': len(self._prices),
                'last_error': self._last_error,
            }

    def wait_for(self, symbol: str, timeout: float = 10.0, poll: float = 0.2) -> Optional[float]:
        """Block until streamer publishes a mid for symbol (or timeout)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            price = self.get(symbol)
            if price is not None:
                return price
            time.sleep(poll)
        return None


def write_mqtt_cache_health(cache: MqttPriceCache, root: Optional[str] = None) -> None:
    """Atomically publish stop_monitor MQTT cache health for launcher/dashboard."""
    payload = cache.cache_health()
    path = ops_path(MQTT_CACHE_HEALTH_FILE, root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(payload, f, indent=2)
        f.flush()
    os.replace(tmp, path)


def mqtt_cache_is_stale(prices) -> bool:
    """True only when cache exposes is_stale() and it returns literal True."""
    checker = getattr(prices, 'is_stale', None)
    if not callable(checker):
        return False
    return checker() is True


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
