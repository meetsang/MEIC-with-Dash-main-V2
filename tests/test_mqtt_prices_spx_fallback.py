"""MQTT cache must not return SPX index price for option symbol lookups."""
import time

from common.mqtt_prices import MqttPriceCache


def test_option_lookup_does_not_fall_back_to_spx():
    cache = MqttPriceCache()
    now = time.time()
    with cache._lock:
        cache._prices['SPX'] = 7504.30
        cache._last_msg_at = now

    assert cache.get_market_mid('.SPXW260701P7460') is None
    assert cache.get('.SPXW260701P7460') is None
    assert cache.get_spx() == 7504.30
