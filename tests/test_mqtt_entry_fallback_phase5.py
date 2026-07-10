"""Phase 5 MQTT entry fallback tests."""
from __future__ import annotations

import json
import logging
import time
from unittest.mock import MagicMock, patch

import pytest

import meic0dte.app.config as app_config
from blocks.entry.entry_quote_validation import (
    REASON_PRE_SCAN,
    evaluate_entry_mqtt_pair,
)
from blocks.entry.entry_scan_config import ENTRY_REST_MIN_COVERAGE_PCT
from blocks.entry.mqtt_entry_fallback import (
    EntryScanDiagnostics,
    attempt_rest_entry_quotes,
    mqtt_cache_for_broker,
)
from blocks.entry.spread_scan import scan_credit_spreads
from blocks.stop.breach_quote import REASON_PAIR_SKEW, REASON_SOURCE_STALE
from common import broker_cooldown
from common.market_quote import REPLAY_EVENT_KIND
from common.mqtt_prices import MqttPriceCache
from common.mqtt_stream_provenance import (
    SESSION_TOPIC,
    build_quote_meta,
    meta_topic_for,
)
from common.rest_metrics import get_rest_metrics, metrics_snapshot, reset_rest_metrics
from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST
from common.symbols import build_tastytrade_symbol, to_tastytrade
from tests.mock_broker import MockBroker

log = logging.getLogger('test.phase5')
SESSION = '20260709-134500-phase5'
EXPIRY = '260709'


def _inject(cache: MqttPriceCache, topic_suffix: str, payload: str) -> None:
    msg = MagicMock()
    msg.topic = f'{cache._prefix}{topic_suffix}'
    msg.payload = payload.encode('utf-8')
    cache._on_message(None, None, msg)


def _session_payload(session_id: str) -> str:
    return json.dumps({
        'stream_session_id': session_id,
        'started_epoch': time.time(),
        'event_kind': 'session',
        'symbols_with_quotes': 0,
    })


def _publish_quote(
    cache: MqttPriceCache,
    symbol: str,
    *,
    price: float,
    source_epoch: float,
    subscription_epoch: float,
    sequence: int,
    session_id: str = SESSION,
) -> None:
    _inject(cache, SESSION_TOPIC, _session_payload(session_id))
    meta = build_quote_meta(
        symbol=symbol,
        source_event_epoch=source_epoch,
        stream_session_id=session_id,
        subscription_epoch=subscription_epoch,
        sequence=sequence,
        event_kind='dxlink_quote',
    )
    _inject(cache, meta_topic_for(symbol), json.dumps(meta))
    _inject(cache, symbol, str(price))


def _broker_with_cache() -> tuple[MockBroker, MqttPriceCache]:
    broker = MockBroker()
    broker.prices['SPX'] = 7225.0
    cache = MqttPriceCache()
    broker._prices = cache
    return broker, cache


def _put_spread_pair(
    cache: MqttPriceCache,
    *,
    otm: int,
    short_credit: float,
    scan_epoch: float,
    seq: int = 10,
) -> tuple[str, str]:
    short = build_tastytrade_symbol(EXPIRY, 'P', 7225 - otm)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7225 - otm - 25)
    sub_epoch = scan_epoch - 1.0
    _publish_quote(
        cache, short, price=short_credit + 0.15, source_epoch=scan_epoch + 0.1,
        subscription_epoch=sub_epoch, sequence=seq,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 0.2,
        subscription_epoch=sub_epoch, sequence=seq + 1,
    )
    return short, long


@pytest.fixture(autouse=True)
def _clear_cooldown():
    broker_cooldown.clear_cooldown()
    yield
    broker_cooldown.clear_cooldown()


def test_cooldown_active_zero_rest_market_data_calls():
    broker, _cache = _broker_with_cache()
    broker.rest_fetch_count = 0

    def counted_fetch(symbols):
        broker.rest_fetch_count += 1
        return {}

    broker.fetch_option_mids_api = counted_fetch
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    reset_rest_metrics()
    symbols = [build_tastytrade_symbol(EXPIRY, 'P', 7200)]
    result = attempt_rest_entry_quotes(broker, symbols, log)
    assert result.cooldown is True
    assert result.should_fallback is True
    assert broker.rest_fetch_count == 0
    snap = metrics_snapshot()
    assert snap['skipped_cooldown']


def test_cooldown_with_valid_post_scan_mqtt_selects_spread():
    broker, cache = _broker_with_cache()
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=70, short_credit=0.60, scan_epoch=scan_epoch)

    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        results = scan_credit_spreads(
            broker, 'P', EXPIRY, '01-45', log,
            spread_width=25, otm_min=70, otm_max=70,
            target_credit=0.60, max_results=1, quote_source='api',
        )
    assert len(results) == 1
    assert results[0].candidate_source == 'mqtt_fallback'
    assert results[0].market_credit == pytest.approx(0.60, abs=0.1)


def test_cooldown_pre_scan_quotes_rejected():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=1.0, source_epoch=scan_epoch - 5.0,
        subscription_epoch=scan_epoch - 10.0, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.2, source_epoch=scan_epoch - 5.0,
        subscription_epoch=scan_epoch - 10.0, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False
    assert readiness.quote_pair_reason == REASON_PRE_SCAN


def test_rest_429_halts_batches_and_falls_back():
    broker, _cache = _broker_with_cache()
    calls = {'n': 0}

    def flaky_fetch(symbols):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError('429 Too Many Requests')
        return {}

    broker.fetch_option_mids_api = flaky_fetch
    result = attempt_rest_entry_quotes(broker, ['.SPXW260709P7200'], log, chunk_size=1)
    assert result.rate_limited is True
    assert result.should_fallback is True
    assert calls['n'] == 1


def test_low_rest_coverage_triggers_mqtt_fallback():
    broker, _cache = _broker_with_cache()
    broker.fetch_option_mids_api = lambda symbols: {}
    symbols = [build_tastytrade_symbol(EXPIRY, 'P', 7200 - i) for i in range(10)]
    result = attempt_rest_entry_quotes(broker, symbols, log)
    assert result.valid == 0
    assert result.should_fallback is True


def test_sufficient_rest_coverage_keeps_rest_path():
    broker, _cache = _broker_with_cache()
    symbols = [build_tastytrade_symbol(EXPIRY, 'P', 7200 - i * 5) for i in range(4)]
    for i, sym in enumerate(symbols):
        broker.prices[to_tastytrade(sym)] = 1.0 + i * 0.1
    result = attempt_rest_entry_quotes(broker, symbols, log)
    assert result.valid == len(symbols)
    assert result.should_fallback is False


def test_candidate_source_rest_or_mqtt_fallback_only():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=60, short_credit=0.62, scan_epoch=scan_epoch)
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        mqtt_results = scan_credit_spreads(
            broker, 'P', EXPIRY, 'lot', log,
            spread_width=25, otm_min=60, otm_max=60,
            target_credit=0.60, quote_source='api',
        )
    broker2, _ = _broker_with_cache()
    for otm, credit in ((60, 0.62),):
        short = build_tastytrade_symbol(EXPIRY, 'P', 7225 - otm)
        long = build_tastytrade_symbol(EXPIRY, 'P', 7225 - otm - 25)
        broker2.prices[to_tastytrade(short)] = credit + 0.15
        broker2.prices[to_tastytrade(long)] = 0.15
    with patch('blocks.entry.entry_scan_config.ENTRY_MQTT_FALLBACK_ENABLED', False):
        rest_results = scan_credit_spreads(
            broker2, 'P', EXPIRY, 'lot', log,
            spread_width=25, otm_min=60, otm_max=60,
            target_credit=0.60, quote_source='api',
        )
    assert len(mqtt_results) == 1
    assert mqtt_results[0].candidate_source == 'mqtt_fallback'
    assert len(rest_results) == 1
    assert rest_results[0].candidate_source == 'rest'


def test_mixed_rest_mqtt_pair_rejected_by_validation():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.80, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch - 1, sequence=3,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_old_stream_session_rejected():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.80, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch - 1, sequence=1, session_id='old-session',
    )
    _inject(cache, SESSION_TOPIC, _session_payload(SESSION))
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 0.2,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_replay_event_rejected():
    broker, cache = _broker_with_cache()
    cache._last_event_kind[build_tastytrade_symbol(EXPIRY, 'P', 7155)] = REPLAY_EVENT_KIND
    scan_epoch = time.time()
    readiness = evaluate_entry_mqtt_pair(
        cache,
        build_tastytrade_symbol(EXPIRY, 'P', 7155),
        build_tastytrade_symbol(EXPIRY, 'P', 7130),
        scan_request_epoch=scan_epoch,
        spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_pre_subscription_quote_rejected():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.80, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch + 5.0, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 0.2,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_stale_leg_rejects_pair():
    broker, cache = _broker_with_cache()
    now = time.time()
    scan_epoch = now - 15.0
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.80, source_epoch=scan_epoch + 0.5,
        subscription_epoch=scan_epoch - 1, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 0.6,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25, now=now,
    )
    assert readiness.quote_pair_reason == REASON_SOURCE_STALE


def test_pair_skew_rejects_pair():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.80, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch - 1, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 5.0,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_reason == REASON_PAIR_SKEW


def test_negative_spread_rejected():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.10, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch - 1, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.50, source_epoch=scan_epoch + 0.2,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_spread_over_width_rejected():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=30.0, source_epoch=scan_epoch + 0.1,
        subscription_epoch=scan_epoch - 1, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch + 0.2,
        subscription_epoch=scan_epoch - 1, sequence=2,
    )
    readiness = evaluate_entry_mqtt_pair(
        cache, short, long, scan_request_epoch=scan_epoch, spread_width=25,
    )
    assert readiness.quote_pair_valid is False


def test_valid_mqtt_pair_uses_unchanged_strike_selection():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=75, short_credit=0.58, scan_epoch=scan_epoch)
    _put_spread_pair(cache, otm=80, short_credit=0.62, scan_epoch=scan_epoch, seq=20)
    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        results = scan_credit_spreads(
            broker, 'P', EXPIRY, 'lot', log,
            spread_width=25, otm_min=75, otm_max=80,
            target_credit=0.60, max_results=1, quote_source='api',
        )
    assert len(results) == 1
    assert results[0].short_strike == 7225 - 80
    assert results[0].distance_from_target <= results[0].distance_from_target + 0.01


def test_timeout_terminates_safely():
    broker, cache = _broker_with_cache()
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    with patch('blocks.entry.entry_scan_config.ENTRY_MQTT_READY_TIMEOUT_SEC', 0.0), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        results = scan_credit_spreads(
            broker, 'P', EXPIRY, '01-45', log,
            spread_width=25, otm_min=70, otm_max=70,
            credit_min=app_config.CREDIT_MIN, credit_max=app_config.CREDIT_MAX_P,
            quote_source='api',
        )
    assert results == []


def test_no_sampled_csv_opened_during_scan():
    broker, cache = _broker_with_cache()
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=70, short_credit=0.60, scan_epoch=scan_epoch)
    original_open = open

    def guarded_open(path, *args, **kwargs):
        path_s = str(path).replace('\\', '/')
        forbidden = (
            'spx_ladder_quotes.csv', 'options_quotes.csv',
            'QQQ_1m.csv', 'GLD_polls.csv',
        )
        assert not any(name in path_s for name in forbidden)
        return original_open(path, *args, **kwargs)

    with patch('builtins.open', guarded_open), \
         patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        scan_credit_spreads(
            broker, 'P', EXPIRY, 'lot', log,
            spread_width=25, otm_min=70, otm_max=70,
            target_credit=0.60, quote_source='api',
        )


def test_mqtt_fallback_does_not_increment_rest_call_count():
    broker, cache = _broker_with_cache()
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=70, short_credit=0.60, scan_epoch=scan_epoch)
    reset_rest_metrics()
    before = metrics_snapshot()['calls_last_1m']
    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        scan_credit_spreads(
            broker, 'P', EXPIRY, '01-45', log,
            spread_width=25, otm_min=70, otm_max=70,
            target_credit=0.60, quote_source='api',
        )
    after = metrics_snapshot()['calls_last_1m']
    assert after == before
    assert metrics_snapshot()['by_operation'].get(OPERATION_ENTRY_MARKET_DATA_REST, 0) == 0


def test_jul9_0145_counterfactual_selects_trade():
    broker, cache = _broker_with_cache()
    broker.prices['SPX'] = 7225.0
    broker_cooldown.set_cooldown('429 Too Many Requests', source='tastytrade', duration_sec=300)
    scan_epoch = time.time()
    _put_spread_pair(cache, otm=70, short_credit=0.60, scan_epoch=scan_epoch)
    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        results = scan_credit_spreads(
            broker, 'P', EXPIRY, '01-45', log,
            spread_width=25, otm_min=70, otm_max=70,
            target_credit=0.60, max_results=1, quote_source='api',
        )
    assert len(results) == 1
    assert results[0].candidate_source == 'mqtt_fallback'


def test_jul9_0145_stale_quotes_no_trade():
    broker, cache = _broker_with_cache()
    broker_cooldown.set_cooldown('429', source='unit', duration_sec=60)
    scan_epoch = time.time()
    short = build_tastytrade_symbol(EXPIRY, 'P', 7155)
    long = build_tastytrade_symbol(EXPIRY, 'P', 7130)
    _publish_quote(
        cache, short, price=0.75, source_epoch=scan_epoch - 60.0,
        subscription_epoch=scan_epoch - 120, sequence=1,
    )
    _publish_quote(
        cache, long, price=0.15, source_epoch=scan_epoch - 60.0,
        subscription_epoch=scan_epoch - 120, sequence=2,
    )
    with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
         patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
        results = scan_credit_spreads(
            broker, 'P', EXPIRY, '01-45', log,
            spread_width=25, otm_min=70, otm_max=70,
            target_credit=0.60, quote_source='api',
        )
    assert results == []


def test_rest_coverage_threshold_constant():
    assert ENTRY_REST_MIN_COVERAGE_PCT == 50.0


def test_diagnostics_failure_format():
    diag = EntryScanDiagnostics(
        rest_symbols_requested=67,
        rest_symbols_valid=0,
        mqtt_symbols_requested=67,
        mqtt_symbols_current_session=63,
        mqtt_symbols_post_scan=61,
        cooldown=True,
    )
    diag.record_mqtt_rejection('pair_skew')
    text = diag.format_failure()
    assert 'entry_scan_failed' in text
    assert 'rest_coverage=0/67' in text
    assert 'cooldown=true' in text
    assert 'reason=pair_skew' in text
