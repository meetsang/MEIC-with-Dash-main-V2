"""Phase 3 MQTT source provenance — cache and streamer contract tests."""
from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import pytest

from common.market_quote import REPLAY_EVENT_KIND
from common.mqtt_prices import MqttPriceCache
from common.mqtt_stream_provenance import (
    HEARTBEAT_TOPIC,
    SESSION_TOPIC,
    StreamPublishState,
    build_quote_meta,
    legacy_republish_enabled,
    make_stream_session_id,
    meta_topic_for,
)


SYMBOL = '.SPXW260709P7530'
SESSION_A = '20260709-083000-11111'
SESSION_B = '20260709-090000-22222'


def _inject(cache: MqttPriceCache, topic_suffix: str, payload: str) -> None:
    msg = MagicMock()
    msg.topic = f'{cache._prefix}{topic_suffix}'
    msg.payload = payload.encode('utf-8')
    cache._on_message(None, None, msg)


def _session_payload(session_id: str) -> str:
    return json.dumps(
        {
            'stream_session_id': session_id,
            'started_epoch': time.time(),
            'event_kind': 'session',
            'symbols_with_quotes': 0,
        }
    )


def _quote_meta(
    *,
    session_id: str,
    source_epoch: float,
    subscription_epoch: float,
    sequence: int,
    event_kind: str = 'dxlink_quote',
) -> dict:
    return build_quote_meta(
        symbol=SYMBOL,
        source_event_epoch=source_epoch,
        stream_session_id=session_id,
        subscription_epoch=subscription_epoch,
        sequence=sequence,
        event_kind=event_kind,
    )


def _publish_genuine_pair(
    cache: MqttPriceCache,
    *,
    session_id: str,
    source_epoch: float,
    subscription_epoch: float,
    sequence: int,
    price: float = 1.25,
    meta_first: bool = True,
) -> None:
    _inject(cache, SESSION_TOPIC, _session_payload(session_id))
    meta = _quote_meta(
        session_id=session_id,
        source_epoch=source_epoch,
        subscription_epoch=subscription_epoch,
        sequence=sequence,
    )
    if meta_first:
        _inject(cache, meta_topic_for(SYMBOL), json.dumps(meta))
        _inject(cache, SYMBOL, str(price))
    else:
        _inject(cache, SYMBOL, str(price))
        _inject(cache, meta_topic_for(SYMBOL), json.dumps(meta))


def test_genuine_update_advances_source_timestamp_and_sequence():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 30.0
    source_a = time.time() - 5.0
    source_b = source_a + 2.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_a,
        subscription_epoch=sub_epoch,
        sequence=1,
    )
    snap1 = cache.get_quote(SYMBOL)
    assert snap1 is not None
    assert snap1.source_event_epoch == pytest.approx(source_a, abs=0.001)
    assert snap1.sequence == 1

    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_b,
        subscription_epoch=sub_epoch,
        sequence=2,
    )
    snap2 = cache.get_quote(SYMBOL)
    assert snap2 is not None
    assert snap2.sequence == 2
    assert snap2.source_event_epoch == pytest.approx(source_b, abs=0.001)
    assert snap2.source_event_epoch > snap1.source_event_epoch


def test_heartbeat_does_not_advance_symbol_freshness():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 30.0
    source_epoch = time.time() - 8.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=sub_epoch,
        sequence=3,
    )
    before = cache.get_quote(SYMBOL)
    assert before is not None

    _inject(
        cache,
        HEARTBEAT_TOPIC,
        json.dumps(
            {
                'stream_session_id': SESSION_A,
                'published_epoch': time.time(),
                'event_kind': 'heartbeat',
                'symbols_with_quotes': 1,
            }
        ),
    )
    after = cache.get_quote(SYMBOL)
    assert after is not None
    assert after.source_event_epoch == before.source_event_epoch
    assert after.sequence == before.sequence


def test_legacy_republish_does_not_advance_source_freshness():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 30.0
    source_epoch = time.time() - 4.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=sub_epoch,
        sequence=10,
        price=2.10,
    )
    before = cache.get_quote(SYMBOL)
    assert before is not None

    replay_meta = {
        **_quote_meta(
            session_id=SESSION_A,
            source_epoch=source_epoch,
            subscription_epoch=sub_epoch,
            sequence=10,
        ),
        'event_kind': REPLAY_EVENT_KIND,
        'published_epoch': time.time(),
    }
    _inject(cache, meta_topic_for(SYMBOL), json.dumps(replay_meta))
    _inject(cache, SYMBOL, '2.15')

    after = cache.get_quote(SYMBOL)
    assert after is not None
    assert after.source_event_epoch == before.source_event_epoch
    assert after.sequence == before.sequence
    assert after.price == pytest.approx(2.10)


def test_retained_old_session_quote_rejected():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 30.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=time.time() - 2.0,
        subscription_epoch=sub_epoch,
        sequence=1,
    )
    assert cache.get_quote(SYMBOL) is not None

    _inject(cache, SESSION_TOPIC, _session_payload(SESSION_B))
    assert cache.get_quote(SYMBOL) is None


def test_current_session_quote_accepted():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 30.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=time.time() - 1.0,
        subscription_epoch=sub_epoch,
        sequence=5,
    )
    snap = cache.get_quote(SYMBOL)
    assert snap is not None
    assert snap.stream_session_id == SESSION_A


def test_pre_subscription_quote_rejected_in_strict_validation():
    cache = MqttPriceCache()
    subscription_epoch = time.time()
    source_epoch = subscription_epoch - 5.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=subscription_epoch,
        sequence=1,
    )
    assert cache.get_quote(SYMBOL) is None
    assert cache.get(SYMBOL) == pytest.approx(1.25)


def test_scalar_only_legacy_consumers_still_receive_prices():
    cache = MqttPriceCache()
    cache._client = MagicMock()
    cache._connected = True
    _inject(cache, SYMBOL, '0.88')
    assert cache.get(SYMBOL) == pytest.approx(0.88)
    assert cache.get_market_mid(SYMBOL) == pytest.approx(0.88)
    assert cache.get_quote(SYMBOL) is None


def test_replay_scalar_does_not_notify_tick_listeners():
    cache = MqttPriceCache()
    seen = []

    def listener(symbol, price, epoch):
        seen.append((symbol, price, epoch))

    cache.add_tick_listener(listener)
    sub_epoch = time.time() - 30.0
    source_epoch = time.time() - 3.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=sub_epoch,
        sequence=1,
    )
    assert len(seen) == 1
    assert seen[0][2] == pytest.approx(source_epoch, abs=0.001)

    replay_meta = {
        **_quote_meta(
            session_id=SESSION_A,
            source_epoch=source_epoch,
            subscription_epoch=sub_epoch,
            sequence=1,
        ),
        'event_kind': REPLAY_EVENT_KIND,
    }
    _inject(cache, meta_topic_for(SYMBOL), json.dumps(replay_meta))
    _inject(cache, SYMBOL, '9.99')
    assert len(seen) == 1


def test_streamer_restart_generates_new_session_id():
    first = StreamPublishState()
    second = StreamPublishState()
    assert first.stream_session_id != second.stream_session_id


def test_make_stream_session_id_unique_per_call():
    a = make_stream_session_id()
    b = make_stream_session_id()
    assert a != b


def test_metadata_before_and_after_scalar_handled_safely():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 20.0
    source_epoch = time.time() - 2.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=sub_epoch,
        sequence=7,
        meta_first=True,
    )
    snap_before = cache.get_quote(SYMBOL)
    assert snap_before is not None
    assert snap_before.sequence == 7

    cache2 = MqttPriceCache()
    _publish_genuine_pair(
        cache2,
        session_id=SESSION_A,
        source_epoch=source_epoch + 1.0,
        subscription_epoch=sub_epoch,
        sequence=8,
        price=3.33,
        meta_first=False,
    )
    snap_after = cache2.get_quote(SYMBOL)
    assert snap_after is not None
    assert snap_after.sequence == 8
    assert snap_after.price == pytest.approx(3.33)


def test_legacy_republish_disabled_by_default(monkeypatch):
    monkeypatch.delenv('TT_LEGACY_REPUBLISH_LAST_MIDS', raising=False)
    assert legacy_republish_enabled() is False


def test_legacy_republish_disabled_skips_scalar_replay():
    state = StreamPublishState()
    state.record_genuine(
        SYMBOL,
        1.5,
        build_quote_meta(
            symbol=SYMBOL,
            source_event_epoch=time.time(),
            stream_session_id=state.stream_session_id,
            subscription_epoch=time.time() - 10,
            sequence=1,
            event_kind='dxlink_quote',
        ),
    )

    def _count_replay_symbols() -> int:
        if not legacy_republish_enabled():
            return 0
        return len(state.last_mids)

    assert _count_replay_symbols() == 0


def test_stream_publish_state_sequence_monotonic():
    state = StreamPublishState()
    state.note_subscriptions([SYMBOL])
    seq1 = state.next_sequence(SYMBOL)
    seq2 = state.next_sequence(SYMBOL)
    assert seq1 == 1
    assert seq2 == 2


def test_get_quote_freshness_uses_source_not_receipt_time():
    cache = MqttPriceCache()
    sub_epoch = time.time() - 60.0
    source_epoch = time.time() - 45.0
    _publish_genuine_pair(
        cache,
        session_id=SESSION_A,
        source_epoch=source_epoch,
        subscription_epoch=sub_epoch,
        sequence=1,
    )
    snap = cache.get_quote(SYMBOL)
    assert snap is not None
    assert snap.source_age_sec >= 40.0
    with cache._lock:
        assert cache._last_symbol_at[SYMBOL] == pytest.approx(source_epoch, abs=0.001)
