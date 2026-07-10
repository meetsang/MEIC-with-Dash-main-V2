"""Phase 4 fill-time software-breach safety tests."""
from __future__ import annotations

import json
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from blocks.stop import state as state_mod
from blocks.stop.breach_config import (
    BREACH_CONFIRM_MAX_WINDOW_SEC,
    BREACH_CONFIRM_OBSERVATIONS,
    BREACH_FILL_GRACE_SEC,
)
from blocks.stop.breach_quote import (
    REASON_CONFIRMATION_PENDING,
    REASON_FILL_GRACE,
    REASON_MISSING_QUOTE,
    REASON_NEGATIVE_SPREAD,
    REASON_OLD_SESSION,
    REASON_PAIR_SKEW,
    REASON_PRE_FILL,
    REASON_PRE_SUBSCRIPTION,
    REASON_READY,
    REASON_REPLAY_EVENT,
    REASON_SOURCE_STALE,
    REASON_SPREAD_OVER_WIDTH,
    evaluate_quote_pair_readiness,
    evaluate_software_breach_exit,
    reset_breach_confirmation,
    update_breach_confirmation,
)
from blocks.stop.fill_reference import ensure_fill_reference_epoch
from blocks.stop.monitor import StopMonitor
from blocks.stop.phases import Phase1InitialStop, PhaseAction
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import TradeSlot
from common.market_quote import REPLAY_EVENT_KIND
from common.mqtt_prices import MqttPriceCache
from common.mqtt_stream_provenance import (
    HEARTBEAT_TOPIC,
    SESSION_TOPIC,
    build_quote_meta,
    meta_topic_for,
)
from tests.mock_broker import MockBroker
from tests.test_v3_paper_scenarios import _mock_prices, _open_state


SESSION = '20260709-105900-11111'
SESSION_B = '20260709-120000-22222'
JUL9_FILL_REF = 1783612760.5913947


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


def _publish_genuine(
    cache: MqttPriceCache,
    symbol: str,
    *,
    session_id: str,
    source_epoch: float,
    subscription_epoch: float,
    sequence: int,
    price: float,
    event_kind: str = 'dxlink_quote',
) -> None:
    _inject(cache, SESSION_TOPIC, _session_payload(session_id))
    meta = build_quote_meta(
        symbol=symbol,
        source_event_epoch=source_epoch,
        stream_session_id=session_id,
        subscription_epoch=subscription_epoch,
        sequence=sequence,
        event_kind=event_kind,
    )
    _inject(cache, meta_topic_for(symbol), json.dumps(meta))
    _inject(cache, symbol, str(price))


def _trade_state(
    *,
    short_sym: str = '.SPXW260709P7495',
    long_sym: str = '.SPXW260709P7470',
    fill_ref: float = JUL9_FILL_REF,
    net_credit: float = 1.05,
) -> dict:
    st = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot='11-00',
        side='P',
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=7495,
        long_strike=7470,
        short_fill=1.85,
        long_fill=0.80,
        net_credit=net_credit,
        quantity=1,
        open_order_id='482267010',
    )
    st['active_stop'] = {
        'order_id': '482267049',
        'type': 'STOP_LIMIT',
        'stop_price': 3.5,
        'limit_price': 3.6,
        'phase': 1,
        'status': 'working',
        'quantity': 1,
    }
    st['stop_quantity'] = 1
    st['lifecycle'] = {
        'fill_reference_epoch': fill_ref,
        'fill_reference_source': 'open_order_last_sync',
    }
    st['open_order'] = {
        'status': 'filled',
        'last_sync_epoch': fill_ref,
        'fully_filled': True,
    }
    return st


def _monitor(state: dict, cache: MqttPriceCache, broker: MockBroker | None = None) -> StopMonitor:
    broker = broker or MockBroker()
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, 't.json')
    state_mod.save_state(path, state)
    with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp):
        mon = StopMonitor(path, broker, cache)
        mon.state = state
        return mon


# --- Phase 3 metadata contract (pre-breach) ---


def test_source_event_epoch_is_unix_seconds_not_milliseconds():
    meta = build_quote_meta(
        symbol='SPX',
        source_event_epoch=1783622400.123,
        stream_session_id=SESSION,
        subscription_epoch=1783622200.0,
        sequence=1,
        event_kind='dxlink_quote',
    )
    assert meta['source_event_epoch'] < 1e11


def test_genuine_event_advances_source_epoch_and_sequence():
    cache = MqttPriceCache()
    sub = time.time() - 60
    src1 = time.time() - 5
    src2 = src1 + 1.0
    _publish_genuine(
        cache, '.SPXW260709P7495',
        session_id=SESSION, source_epoch=src1, subscription_epoch=sub, sequence=1, price=1.8,
    )
    q1 = cache.get_quote('.SPXW260709P7495')
    assert q1 is not None
    assert q1.sequence == 1
    _publish_genuine(
        cache, '.SPXW260709P7495',
        session_id=SESSION, source_epoch=src2, subscription_epoch=sub, sequence=2, price=1.9,
    )
    q2 = cache.get_quote('.SPXW260709P7495')
    assert q2 is not None
    assert q2.sequence == 2
    assert q2.source_event_epoch > q1.source_event_epoch


def test_heartbeat_does_not_advance_source_epoch():
    cache = MqttPriceCache()
    sub = time.time() - 60
    src = time.time() - 4
    _publish_genuine(
        cache, '.SPXW260709P7495',
        session_id=SESSION, source_epoch=src, subscription_epoch=sub, sequence=3, price=2.0,
    )
    before = cache.get_quote('.SPXW260709P7495')
    _inject(
        cache,
        HEARTBEAT_TOPIC,
        json.dumps(
            {
                'stream_session_id': SESSION,
                'published_epoch': time.time(),
                'event_kind': 'heartbeat',
                'symbols_with_quotes': 1,
            }
        ),
    )
    after = cache.get_quote('.SPXW260709P7495')
    assert after is not None and before is not None
    assert after.source_event_epoch == before.source_event_epoch
    assert after.sequence == before.sequence


def test_replay_does_not_advance_source_epoch():
    cache = MqttPriceCache()
    sub = time.time() - 60
    src = time.time() - 3
    sym = '.SPXW260709P7495'
    _publish_genuine(cache, sym, session_id=SESSION, source_epoch=src, subscription_epoch=sub, sequence=5, price=2.1)
    before = cache.get_quote(sym)
    replay_meta = build_quote_meta(
        symbol=sym,
        source_event_epoch=src,
        stream_session_id=SESSION,
        subscription_epoch=sub,
        sequence=5,
        event_kind=REPLAY_EVENT_KIND,
    )
    _inject(cache, meta_topic_for(sym), json.dumps(replay_meta))
    _inject(cache, sym, '9.99')
    after = cache.get_quote(sym)
    assert after is not None and before is not None
    assert after.source_event_epoch == before.source_event_epoch
    assert after.price == pytest.approx(2.1)


def test_stream_session_id_must_match_current_session():
    cache = MqttPriceCache()
    sub = time.time() - 60
    _publish_genuine(
        cache, '.SPXW260709P7495',
        session_id=SESSION, source_epoch=time.time() - 2, subscription_epoch=sub, sequence=1, price=1.5,
    )
    assert cache.get_quote('.SPXW260709P7495') is not None
    _inject(cache, SESSION_TOPIC, _session_payload(SESSION_B))
    assert cache.get_quote('.SPXW260709P7495') is None


# --- Phase 4 readiness ---


def test_no_software_breach_during_fill_grace():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time())
    sub = time.time() - 120
    now = time.time()
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now, subscription_epoch=sub, sequence=1, price=3.7,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now, subscription_epoch=sub, sequence=1, price=0.8,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_FILL_GRACE
    assert readiness.software_breach_ready is False


def test_pre_fill_quote_pair_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=JUL9_FILL_REF - 5, subscription_epoch=sub, sequence=1, price=3.7,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=JUL9_FILL_REF - 5, subscription_epoch=sub, sequence=1, price=0.8,
    )
    readiness = evaluate_quote_pair_readiness(
        state, cache, now=JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 1,
    )
    assert readiness.quote_pair_reason == REASON_PRE_FILL


def test_pre_subscription_quote_pair_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=JUL9_FILL_REF + 1, subscription_epoch=sub, sequence=1, price=1.8,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=JUL9_FILL_REF + 1, subscription_epoch=sub, sequence=1, price=0.8,
    )
    readiness = evaluate_quote_pair_readiness(
        state, cache, now=JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 10,
    )
    assert readiness.quote_pair_reason == REASON_PRE_SUBSCRIPTION


def test_old_stream_session_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=1.8,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION_B, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=0.8,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_OLD_SESSION


def test_replay_metadata_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    sym_s = state['short_leg']['symbol']
    sym_l = state['long_leg']['symbol']
    for sym, price in ((sym_s, 3.7), (sym_l, 0.8)):
        meta = build_quote_meta(
            symbol=sym,
            source_event_epoch=now - 1,
            stream_session_id=SESSION,
            subscription_epoch=sub,
            sequence=1,
            event_kind=REPLAY_EVENT_KIND,
        )
        _inject(cache, SESSION_TOPIC, _session_payload(SESSION))
        _inject(cache, meta_topic_for(sym), json.dumps(meta))
        _inject(cache, sym, str(price))
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_REPLAY_EVENT


def test_one_stale_leg_rejects_pair():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 10, subscription_epoch=sub, sequence=1, price=1.8,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=0.8,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_SOURCE_STALE


def test_pair_skew_beyond_limit_rejects_pair():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=3.0,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 5, subscription_epoch=sub, sequence=1, price=0.5,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_PAIR_SKEW


def test_negative_spread_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=0.5,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=1.0,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_NEGATIVE_SPREAD


def test_spread_over_width_rejected():
    cache = MqttPriceCache()
    state = _trade_state()
    sub = JUL9_FILL_REF - 120
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=30.0,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=0.5,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    assert readiness.quote_pair_reason == REASON_SPREAD_OVER_WIDTH


# --- Confirmation ---


def _ready_breached_pair(cache: MqttPriceCache, state: dict, base_now: float, seq: int) -> None:
    fill_ref = (state.get('lifecycle') or {}).get('fill_reference_epoch', base_now)
    sub = float(fill_ref) - 120
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=base_now - 0.5, subscription_epoch=sub, sequence=seq, price=3.2,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=base_now - 0.4, subscription_epoch=sub, sequence=seq, price=0.4,
    )


def test_first_breached_observation_increments_but_does_not_act():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time() - 30)
    now = time.time()
    _ready_breached_pair(cache, state, now, seq=1)
    mon = _monitor(state, cache)
    should_exit, readiness, conf = evaluate_software_breach_exit(
        mon, streamer_stale=False, mqtt_cache_stale=False, now=now,
    )
    assert readiness.software_breach_ready
    assert conf['breach_confirmation_count'] == 1
    assert should_exit is False


def test_same_sequences_do_not_count_twice():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time() - 30)
    now = time.time()
    _ready_breached_pair(cache, state, now, seq=2)
    mon = _monitor(state, cache)
    evaluate_software_breach_exit(mon, now=now)
    evaluate_software_breach_exit(mon, now=now)
    conf = state['breach_confirmation']
    assert conf['count'] == 1


def test_second_advancing_observation_triggers_breach():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time() - 30)
    now = time.time()
    _ready_breached_pair(cache, state, now, seq=3)
    mon = _monitor(state, cache)
    evaluate_software_breach_exit(mon, now=now)
    _ready_breached_pair(cache, state, now + 0.5, seq=4)
    should_exit, _, conf = evaluate_software_breach_exit(mon, now=now + 0.5)
    assert conf['breach_confirmation_count'] >= BREACH_CONFIRM_OBSERVATIONS
    assert should_exit is True


def test_non_breached_quote_resets_confirmation():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time() - 30)
    now = time.time()
    _ready_breached_pair(cache, state, now, seq=5)
    mon = _monitor(state, cache)
    evaluate_software_breach_exit(mon, now=now)
    sub = state['lifecycle']['fill_reference_epoch'] - 120
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now + 1, subscription_epoch=sub, sequence=6, price=1.8,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now + 1, subscription_epoch=sub, sequence=6, price=0.8,
    )
    _, _, conf = evaluate_software_breach_exit(mon, now=now + 1)
    assert conf['breach_confirmation_count'] == 0


def test_invalid_quote_resets_confirmation():
    cache = MqttPriceCache()
    state = _trade_state()
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    _ready_breached_pair(cache, state, now, seq=7)
    mon = _monitor(state, cache)
    evaluate_software_breach_exit(mon)
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    conf = update_breach_confirmation(state, readiness, spread_breached=False, now=now)
    assert conf['breach_confirmation_count'] == 0


def test_confirmation_window_expiry_resets():
    now = time.time()
    state = _trade_state(fill_ref=now - 30)
    state['breach_confirmation'] = {
        'count': 1,
        'window_started_at': now - BREACH_CONFIRM_MAX_WINDOW_SEC - 1,
        'last_short_sequence': 1,
        'last_long_sequence': 1,
        'last_short_source_epoch': 1.0,
        'last_long_source_epoch': 1.0,
        'last_stream_session_id': SESSION,
    }
    cache = MqttPriceCache()
    _ready_breached_pair(cache, state, now, seq=10)
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    conf = update_breach_confirmation(state, readiness, spread_breached=True, now=now)
    assert conf['breach_confirmation_count'] == 1


def test_stream_session_change_resets_confirmation():
    state = _trade_state()
    state['breach_confirmation'] = {
        'count': 1,
        'window_started_at': time.time(),
        'last_short_sequence': 1,
        'last_long_sequence': 1,
        'last_short_source_epoch': 1.0,
        'last_long_source_epoch': 1.0,
        'last_stream_session_id': SESSION,
    }
    cache = MqttPriceCache()
    now = JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 5
    sub = JUL9_FILL_REF - 120
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION_B, source_epoch=now - 1, subscription_epoch=sub, sequence=2, price=3.2,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION_B, source_epoch=now - 1, subscription_epoch=sub, sequence=2, price=0.4,
    )
    readiness = evaluate_quote_pair_readiness(state, cache, now=now)
    conf = update_breach_confirmation(state, readiness, spread_breached=True, now=now)
    assert conf['breach_confirmation_count'] == 1


# --- Exchange stop / exit job / display ---


def test_exchange_stop_placed_immediately_during_fill_grace():
    broker = MockBroker()
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time())
    state['active_stop'] = None
    state['stop_quantity'] = 0
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 't.json')
        state_mod.save_state(path, state)
        with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
             patch.object(StopMonitor, '_streamer_prices_stale', return_value=False):
            mon = StopMonitor(path, broker, cache)
            mon._ensure_stop_for_filled_qty_unblocked()
    stops = [p for p in broker.placed if p[0] == 'stop']
    assert len(stops) == 1


def test_existing_exit_job_prevents_duplicate_exit_creation():
    broker = MockBroker()
    prices = _mock_prices()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 't.json')
        st = _open_state(close_only_mode=True, exit_handler='breach_phase1_initial_stop')
        state_mod.save_state(path, st)
        slot = TradeSlot.from_path(path)
        sup = StopSupervisor(broker, prices)
        with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
             patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
             patch.object(sup, '_discover_slots', return_value=[slot]), \
             patch.object(sup, '_sync_pending_fills'), \
             patch.object(sup, '_write_heartbeat'), \
             patch.object(sup.exit_pool, 'has_job', return_value=True), \
             patch.object(StopMonitor, '_refresh_breach_watch_display_only'), \
             patch.object(sup, '_enqueue_confirmed_exit') as mock_exit:
            sup._cycle()
        mock_exit.assert_not_called()


def test_breach_watch_refreshes_during_active_exit_job():
    broker = MockBroker()
    cache = MqttPriceCache()
    state = _trade_state()
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 't.json')
        state_mod.save_state(path, state)
        with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
             patch.object(StopMonitor, '_streamer_prices_stale', return_value=False):
            mon = StopMonitor(path, broker, cache)
            mon.state['close_only_mode'] = True
            mon._refresh_breach_watch_display_only()
            assert mon.state['breach_watch']['updated_at']


# --- Jul 9 11-00_P replay ---


def test_jul9_prefill_stale_spread_does_not_confirm_breach():
    cache = MqttPriceCache()
    fill_ref = time.time() - 20
    state = _trade_state(fill_ref=fill_ref)
    sub = fill_ref - 120
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=fill_ref - 1, subscription_epoch=sub, sequence=1, price=3.7,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=fill_ref - 1, subscription_epoch=sub, sequence=1, price=0.8,
    )
    phase = Phase1InitialStop()
    mon = _monitor(state, cache)
    with patch.object(mon, '_streamer_prices_stale', return_value=False), \
         patch.object(mon, '_mqtt_cache_stale', return_value=False):
        assert phase._exit_required(mon) is False


def test_jul9_post_fill_coherent_quotes_do_not_breach():
    cache = MqttPriceCache()
    fill_ref = time.time() - 30
    state = _trade_state(fill_ref=fill_ref)
    sub = fill_ref - 120
    now = time.time()
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=1.85,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now - 1, subscription_epoch=sub, sequence=1, price=0.80,
    )
    phase = Phase1InitialStop()
    mon = _monitor(state, cache)
    with patch.object(mon, '_streamer_prices_stale', return_value=False), \
         patch.object(mon, '_mqtt_cache_stale', return_value=False):
        assert phase._exit_required(mon) is False


def test_jul9_genuine_later_breach_with_two_advancing_pairs_triggers():
    cache = MqttPriceCache()
    fill_ref = time.time() - 30
    state = _trade_state(fill_ref=fill_ref)
    now = time.time()
    _ready_breached_pair(cache, state, now, seq=20)
    mon = _monitor(state, cache)
    phase = Phase1InitialStop()
    with patch.object(mon, '_streamer_prices_stale', return_value=False), \
         patch.object(mon, '_mqtt_cache_stale', return_value=False):
        evaluate_software_breach_exit(mon, now=now)
        _ready_breached_pair(cache, state, now + 0.5, seq=21)
        assert phase._exit_required(mon) is True


def test_v2_and_v3_use_equivalent_readiness_gate():
    cache = MqttPriceCache()
    state = _trade_state(fill_ref=time.time())
    sub = time.time() - 120
    now = time.time()
    _publish_genuine(
        cache, state['short_leg']['symbol'],
        session_id=SESSION, source_epoch=now, subscription_epoch=sub, sequence=1, price=3.5,
    )
    _publish_genuine(
        cache, state['long_leg']['symbol'],
        session_id=SESSION, source_epoch=now, subscription_epoch=sub, sequence=1, price=0.5,
    )
    mon = _monitor(state, cache)
    with patch.object(mon, '_streamer_prices_stale', return_value=False):
        mon._refresh_breach_watch(streamer_stale=False, mqtt_cache_stale=False)
    assert mon.state['breach_watch']['software_breach_ready'] is False
    assert mon.state['breach_watch']['quote_pair_reason'] == REASON_FILL_GRACE

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 't.json')
        state_mod.save_state(path, state)
        slot = TradeSlot.from_path(path)
        sup = StopSupervisor(MockBroker(), cache)
        with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
             patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
             patch.object(sup, '_drain_alert_fills', return_value=False), \
             patch.object(sup, '_slow_broker_sync', return_value=False), \
             patch.object(sup, 'exit_pool') as pool:
            pool.has_job.return_value = False
            mon2 = sup._legacy_monitor(slot)
            with patch.object(mon2, '_streamer_prices_stale', return_value=False):
                ready, status = sup._breach_arm_ready(slot, mon2)
            assert ready is False
            assert status == 'fill_grace'


def test_legacy_json_without_new_fields_loads_safely():
    state = _trade_state()
    state.pop('lifecycle', None)
    state.pop('breach_confirmation', None)
    ensure_fill_reference_epoch(state)
    readiness = evaluate_quote_pair_readiness(
        state,
        MqttPriceCache(),
        now=JUL9_FILL_REF + BREACH_FILL_GRACE_SEC + 1,
    )
    assert readiness.quote_pair_reason in (REASON_FILL_GRACE, REASON_MISSING_QUOTE, 'no_prices')
