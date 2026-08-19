"""Spread scan via REST API quotes (no MQTT)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from common.symbols import build_tastytrade_symbol, to_tastytrade
from blocks.entry.spread_scan import scan_credit_spreads
from tests.mock_broker import MockBroker


@pytest.fixture(autouse=True)
def _disable_mqtt_entry_fallback():
    with patch('blocks.entry.entry_scan_config.ENTRY_MQTT_FALLBACK_ENABLED', False):
        yield

def test_manual_scan_api_returns_closest_to_target():
    broker = MockBroker()
    broker.prices['SPX'] = 6000.0
    expiry = '260624'
    log = logging.getLogger('test')

    for otm, credit in ((30, 0.55), (60, 0.62), (90, 0.48)):
        short = build_tastytrade_symbol(expiry, 'P', 6000 - otm)
        long = build_tastytrade_symbol(expiry, 'P', 6000 - otm - 25)
        broker.prices[to_tastytrade(short)] = credit + 0.10
        broker.prices[to_tastytrade(long)] = 0.10

    results = scan_credit_spreads(
        broker, 'P', expiry, 'test-lot', log,
        spread_width=25,
        otm_min=5,
        otm_max=100,
        target_credit=0.60,
        max_results=3,
        quote_source='api',
    )
    assert len(results) == 3
    assert results[0].distance_from_target <= results[1].distance_from_target
    assert abs(results[0].market_credit - 0.60) <= abs(results[1].market_credit - 0.60)


def test_manual_scan_finds_far_otm_for_low_target():
    """Near-ATM quotes only (high credit) — low target must reach deeper OTM."""
    broker = MockBroker()
    broker.prices['SPX'] = 7225.0
    expiry = '260626'
    log = logging.getLogger('test')

    # Near ATM: high credit (only these quoted in a partial-API scenario)
    for otm in (10, 15, 20):
        short = build_tastytrade_symbol(expiry, 'P', 7225 - otm)
        long = build_tastytrade_symbol(expiry, 'P', 7225 - otm - 25)
        credit = 1.50 - (otm - 10) * 0.05
        broker.prices[to_tastytrade(short)] = credit + 0.80
        broker.prices[to_tastytrade(long)] = 0.80

    # Far OTM: near target 0.60
    for otm in (70, 75, 80):
        short = build_tastytrade_symbol(expiry, 'P', 7225 - otm)
        long = build_tastytrade_symbol(expiry, 'P', 7225 - otm - 25)
        credit = 0.58 + (otm - 70) * 0.02
        broker.prices[to_tastytrade(short)] = credit + 0.15
        broker.prices[to_tastytrade(long)] = 0.15

    results = scan_credit_spreads(
        broker, 'P', expiry, 'test-lot', log,
        spread_width=25,
        otm_min=5,
        otm_max=300,
        target_credit=0.60,
        max_results=3,
        quote_source='api',
    )
    assert len(results) >= 1
    assert results[0].market_credit <= 0.75
    assert results[0].distance_from_target < 0.20


def test_manual_scan_api_no_streamer_symbols():
    broker = MockBroker()
    broker.prices['SPX'] = 6000.0
    log = logging.getLogger('test')
    results = scan_credit_spreads(
        broker, 'P', '260624', 'test-lot', log,
        spread_width=25,
        otm_min=5,
        otm_max=20,
        target_credit=0.60,
        max_results=3,
        quote_source='api',
    )
    assert results == []


def test_manual_scan_uses_partial_rest_when_mqtt_fallback_empty():
    """Low REST coverage triggers MQTT path, but REST mids still count."""
    broker = MockBroker()
    broker.prices['SPX'] = 6000.0
    broker._prices = MagicMock()  # noqa: SLF001 — triggers mqtt fallback branch
    expiry = '260624'
    log = logging.getLogger('test')
    short = build_tastytrade_symbol(expiry, 'P', 5970)
    long = build_tastytrade_symbol(expiry, 'P', 5945)
    broker.prices[to_tastytrade(short)] = 0.70
    broker.prices[to_tastytrade(long)] = 0.10

    with patch('blocks.entry.entry_scan_config.ENTRY_MQTT_FALLBACK_ENABLED', True), \
         patch('blocks.entry.entry_scan_config.ENTRY_REST_MIN_COVERAGE_PCT', 99.0), \
         patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=0.0), \
         patch('blocks.entry.mqtt_entry_fallback.evaluate_mqtt_entry_pair', return_value=None):
        results = scan_credit_spreads(
            broker, 'P', expiry, 'test-lot', log,
            spread_width=25,
            otm_min=30,
            otm_max=30,
            target_credit=0.60,
            max_results=1,
            quote_source='api',
        )
    assert len(results) == 1
    assert abs(results[0].market_credit - 0.60) <= 0.05
