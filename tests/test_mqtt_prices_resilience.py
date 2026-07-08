"""MQTT cache staleness, reconnect health, and SPX fallback guards."""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from common.mqtt_prices import MqttPriceCache, write_mqtt_cache_health


def _fresh_cache_with_prices() -> MqttPriceCache:
    cache = MqttPriceCache(stale_threshold_sec=30.0)
    cache._client = MagicMock()
    cache._connected = True
    now = time.time()
    with cache._lock:
        cache._prices['SPX'] = 7504.30
        cache._prices['.SPXW260701P7460'] = 0.35
        cache._last_msg_at = now
        cache._last_symbol_at['SPX'] = now
        cache._last_symbol_at['.SPXW260701P7460'] = now
    return cache


def test_option_lookup_does_not_fall_back_to_spx():
    cache = _fresh_cache_with_prices()
    assert cache.get_market_mid('.SPXW260701P7460') == 0.35
    assert cache.get('.SPXW260701P7460') == 0.35
    assert cache.get_spx() == 7504.30


def test_stale_cache_returns_none_for_option_and_spx():
    cache = _fresh_cache_with_prices()
    with cache._lock:
        cache._last_msg_at = time.time() - 120.0
    assert cache.is_stale() is True
    assert cache.get_market_mid('.SPXW260701P7460') is None
    assert cache.get_spx() is None


def test_fresh_cache_returns_prices():
    cache = _fresh_cache_with_prices()
    assert cache.is_stale() is False
    assert cache.get_market_mid('.SPXW260701P7460') == 0.35
    assert cache.get_spx() == 7504.30


def test_override_bypasses_staleness_for_get_only():
    cache = _fresh_cache_with_prices()
    with cache._lock:
        cache._last_msg_at = time.time() - 120.0
    cache.set_override('.SPXW260701P7460', 0.99)
    assert cache.get('.SPXW260701P7460') == 0.99
    assert cache.get_market_mid('.SPXW260701P7460') is None


def test_cache_health_reports_stale():
    cache = _fresh_cache_with_prices()
    with cache._lock:
        cache._last_msg_at = time.time() - 120.0
    health = cache.cache_health()
    assert health['stale'] is True
    assert health['connected'] is True
    assert health['price_count'] == 2


def test_write_mqtt_cache_health_atomic(tmp_path):
    cache = _fresh_cache_with_prices()
    write_mqtt_cache_health(cache, root=str(tmp_path))
    path = tmp_path / 'trades' / 'mqtt_cache_health.json'
    assert path.is_file()
    data = path.read_text(encoding='utf-8')
    assert 'stale' in data
    assert '"connected": true' in data.lower() or '"connected": True' in data


def test_reconnect_updates_connected_state():
    cache = MqttPriceCache()
    cache._client = MagicMock()
    cache._connected = False
    cache._on_connect(cache._client, None, None, 0, None)
    assert cache._connected is True
    cache._on_disconnect(cache._client, None, None, 1, None)
    assert cache._connected is False


def test_on_message_updates_last_msg_at():
    cache = MqttPriceCache()
    msg = MagicMock()
    msg.topic = f'{cache._prefix}SPX'
    msg.payload = b'7480.5'
    cache._on_message(None, None, msg)
    assert cache._last_msg_at > 0
    assert cache.get('SPX') == 7480.5
    cache._client = MagicMock()
    assert cache.get('SPX') == 7480.5
    with cache._lock:
        cache._last_msg_at = time.time() - 120.0
    assert cache.get('SPX') is None


def test_tick_listener_fires_on_message():
    cache = MqttPriceCache()
    seen = []

    def listener(symbol, price, epoch):
        seen.append((symbol, price, epoch))

    cache.add_tick_listener(listener)
    msg = MagicMock()
    msg.topic = f'{cache._prefix}QQQ'
    msg.payload = b'707.25'
    cache._on_message(None, None, msg)
    assert seen == [('QQQ', 707.25, seen[0][2])]
    cache.remove_tick_listener(listener)
    cache._on_message(None, None, msg)
    assert len(seen) == 1

