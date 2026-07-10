"""Integrated REST resilience release-candidate scenario (Phases 1–5)."""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from unittest.mock import MagicMock, patch

import pytest

from blocks.entry.entry_scan_config import ENTRY_MQTT_FALLBACK_ENABLED
from blocks.entry.spread_scan import scan_credit_spreads
from blocks.stop import state as state_mod
from blocks.stop.breach_config import BREACH_CONFIRM_OBSERVATIONS, BREACH_FILL_GRACE_SEC
from blocks.stop.breach_quote import (
    evaluate_quote_pair_readiness,
    evaluate_software_breach_exit,
    update_breach_confirmation,
)
from blocks.stop.fill_sync import sync_open_order
from blocks.stop.monitor import StopMonitor
from blocks.stop.reconcile_policy import simulate_reconcile_events
from blocks.stop.v3.supervisor import StopSupervisor
from blocks.stop.v3.trade_slot import TradeSlot
from brokers.base import OrderResult
from common import broker_cooldown
from common.mqtt_prices import MqttPriceCache
from common.mqtt_stream_provenance import SESSION_TOPIC, build_quote_meta, legacy_republish_enabled, meta_topic_for
from common.rest_metrics import get_rest_metrics, metrics_snapshot, reset_rest_metrics
from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST
from common.symbols import build_tastytrade_symbol, to_tastytrade
from dashboard.server import app, build_summary
from tests.mock_broker import MockBroker
from tests.test_fill_provenance_phase1 import TestJul9MissingLegScenario
from tests.test_mqtt_entry_fallback_phase5 import SESSION, _broker_with_cache, _put_spread_pair
from tests.test_v3_paper_scenarios import _mock_prices, _open_state

log = logging.getLogger('test.rc')
EXPIRY = '260709'
RELEASE_METRICS: dict = {}


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


def _publish_pair(cache, short_sym, long_sym, *, session, source_epoch, sub_epoch, seq, short_p, long_p):
    _inject(cache, SESSION_TOPIC, _session_payload(session))
    for sym, price, sq in ((short_sym, short_p, seq), (long_sym, long_p, seq + 1)):
        meta = build_quote_meta(
            symbol=sym,
            source_event_epoch=source_epoch,
            stream_session_id=session,
            subscription_epoch=sub_epoch,
            sequence=sq,
            event_kind='dxlink_quote',
        )
        _inject(cache, meta_topic_for(sym), json.dumps(meta))
        _inject(cache, sym, str(price))


class TestRestResilienceReleaseCandidate:
    """End-to-end paper checks for the integrated runtime RC."""

    def test_normal_rest_entry(self):
        broker = MockBroker()
        broker.prices['SPX'] = 6000.0
        for otm, credit in ((30, 0.55), (60, 0.62)):
            short = build_tastytrade_symbol(EXPIRY, 'P', 6000 - otm)
            long = build_tastytrade_symbol(EXPIRY, 'P', 6000 - otm - 25)
            broker.prices[to_tastytrade(short)] = credit + 0.10
            broker.prices[to_tastytrade(long)] = 0.10
        with patch('blocks.entry.entry_scan_config.ENTRY_MQTT_FALLBACK_ENABLED', False):
            results = scan_credit_spreads(
                broker, 'P', EXPIRY, 'rc-rest', log,
                spread_width=25, otm_min=30, otm_max=60,
                target_credit=0.60, max_results=1, quote_source='api',
            )
        assert results
        assert results[0].candidate_source == 'rest'

    def test_forced_cooldown_mqtt_fallback(self):
        broker, cache = _broker_with_cache()
        broker_cooldown.set_cooldown('429', source='unit', duration_sec=300)
        scan_epoch = time.time()
        _put_spread_pair(cache, otm=70, short_credit=0.60, scan_epoch=scan_epoch)
        reset_rest_metrics()
        with patch('blocks.entry.mqtt_entry_fallback.prepare_mqtt_entry_fallback', return_value=scan_epoch), \
             patch('blocks.entry.mqtt_entry_fallback.time.sleep'):
            results = scan_credit_spreads(
                broker, 'P', EXPIRY, '01-45', log,
                spread_width=25, otm_min=70, otm_max=70,
                target_credit=0.60, quote_source='api',
            )
        snap = metrics_snapshot()
        assert results[0].candidate_source == 'mqtt_fallback'
        assert snap['by_operation'].get(OPERATION_ENTRY_MARKET_DATA_REST, 0) == 0
        RELEASE_METRICS['entry_rest_avoided_cooldown'] = 1

    def test_missing_leg_protective_estimate(self):
        t = TestJul9MissingLegScenario()
        t.setUp()
        state = t._jul9_state()
        broker = t._broker_missing_long()
        sync_open_order(state, broker, force=True)
        sync_open_order(state, broker, force=True)
        assert state['status'] == 'open'
        assert state['entry']['fill_confidence'] == 'protective_estimate'

    def test_immediate_exchange_stop_placement(self):
        broker = MockBroker()
        cache = MqttPriceCache()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state()
            st['active_stop'] = None
            st['stop_quantity'] = 0
            st['lifecycle'] = {'fill_reference_epoch': time.time()}
            state_mod.save_state(path, st)
            with patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(StopMonitor, '_streamer_prices_stale', return_value=False):
                mon = StopMonitor(path, broker, cache)
                mon._ensure_stop_for_filled_qty_unblocked()
        assert any(p[0] == 'stop' for p in broker.placed)

    def test_fill_grace_and_confirmed_breach(self):
        broker = MockBroker()
        prices = MagicMock()
        cache = MqttPriceCache()
        state = _open_state()
        fill_ref = time.time() - 5.0
        state['lifecycle'] = {'fill_reference_epoch': fill_ref}
        short_sym = state['short_leg']['symbol']
        long_sym = state['long_leg']['symbol']
        session = SESSION
        sub = fill_ref - 30.0

        readiness_grace = evaluate_quote_pair_readiness(
            state, cache, now=fill_ref + BREACH_FILL_GRACE_SEC - 1,
        )
        assert readiness_grace.software_breach_ready is False

        now = fill_ref + BREACH_FILL_GRACE_SEC + 2
        threshold = 3.5
        state['designated_stop_price'] = threshold / 2
        breached_mid = threshold + 0.5
        long_p = 0.20
        short_p = breached_mid + long_p
        _publish_pair(cache, short_sym, long_sym, session=session, source_epoch=now - 1,
                      sub_epoch=sub, seq=10, short_p=short_p, long_p=long_p)
        readiness = evaluate_quote_pair_readiness(state, cache, now=now)
        assert readiness.software_breach_ready
        conf1 = update_breach_confirmation(state, readiness, spread_breached=True, now=now)
        assert conf1['breach_confirmation_count'] == 1
        _publish_pair(cache, short_sym, long_sym, session=session, source_epoch=now,
                      sub_epoch=sub, seq=11, short_p=short_p + 0.01, long_p=long_p)
        readiness2 = evaluate_quote_pair_readiness(state, cache, now=now + 0.5)
        conf2 = update_breach_confirmation(state, readiness2, spread_breached=True, now=now + 0.5)
        assert conf2['software_breach_confirmed']
        assert BREACH_CONFIRM_OBSERVATIONS == 2

    def test_exchange_stop_alert_enqueues_exit_handler(self):
        broker = MockBroker()
        broker.orders['9001'] = OrderResult(
            True, '9001', 'filled', filled_price=1.8, filled_quantity=3, order_quantity=3,
        )
        prices = _mock_prices()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = _open_state(status='open')
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)
            sup = StopSupervisor(broker, prices)
            with patch('blocks.stop.v3.command_claim._trades_root_for_path', return_value=tmp), \
                 patch('blocks.stop.monitor._trades_root_for_path', return_value=tmp), \
                 patch.object(sup.exit_pool, 'submit', return_value=True) as submit:
                mon = sup._legacy_monitor(slot)
                mon.fill_queue.put({'order_id': '9001', 'status': 'filled'})
                assert sup._drain_alert_fills(slot) is True
                submit.assert_called_once()

    def test_old_session_rejected_after_restart(self):
        cache = MqttPriceCache()
        state = _open_state()
        fill_ref = time.time() - 60
        state['lifecycle'] = {'fill_reference_epoch': fill_ref}
        short_sym = state['short_leg']['symbol']
        long_sym = state['long_leg']['symbol']
        _inject(cache, SESSION_TOPIC, _session_payload('old-session'))
        _publish_pair(cache, short_sym, long_sym, session='old-session',
                      source_epoch=time.time(), sub_epoch=fill_ref, seq=1,
                      short_p=2.0, long_p=0.5)
        _inject(cache, SESSION_TOPIC, _session_payload(SESSION))
        readiness = evaluate_quote_pair_readiness(state, cache, now=time.time())
        assert readiness.quote_pair_reason == 'old_session'

    def test_dashboard_summary_zero_broker_calls(self):
        client = app.test_client()
        with patch('dashboard.server._live_price', return_value=None), \
             patch('dashboard.server.build_manual_trades', return_value=([], 0, 0, 0)), \
             patch('dashboard.server._read_active_trades', return_value=[]), \
             patch('dashboard.server.bootstrap_meic_session_if_missing', return_value=None):
            resp = client.get('/api/summary')
        assert resp.status_code == 200

    def test_collect_operational_metrics(self):
        reset_rest_metrics()
        m = get_rest_metrics()
        m.record_call('pending_fill_status', 'HIGH')
        m.record_call('working_stop_reconcile', 'LOW')
        m.record_call('entry_market_data_rest', 'NORMAL')
        m.record_skipped_cooldown('entry_market_data_rest', 'NORMAL')

        trades = []
        for i in range(8):
            st = _open_state(lot=f'{i:02d}-00')
            st['active_stop']['order_id'] = f'oid-{i}'
            trades.append(st)
        before = simulate_reconcile_events(trades, duration_sec=600, fixed_interval_sec=10)
        after = simulate_reconcile_events([copy.deepcopy(t) for t in trades], duration_sec=600)

        RELEASE_METRICS.update({
            'rest_by_operation': metrics_snapshot()['by_operation'],
            'rest_by_priority': metrics_snapshot()['by_priority'],
            'cooldown_skips': metrics_snapshot()['skipped_cooldown'],
            'reconcile_before_peak': before['peak_calls_one_second'],
            'reconcile_after_peak': after['peak_calls_one_second'],
            'reconcile_before_total': before['total_calls'],
            'reconcile_after_total': after['total_calls'],
            'legacy_republish_default': legacy_republish_enabled() is False,
            'entry_fallback_default': ENTRY_MQTT_FALLBACK_ENABLED is True,
            'breach_fill_grace_default': BREACH_FILL_GRACE_SEC == 10.0,
            'breach_confirm_default': BREACH_CONFIRM_OBSERVATIONS == 2,
        })
        assert after['peak_calls_one_second'] < before['peak_calls_one_second']

    def test_production_defaults(self):
        from blocks.entry.entry_scan_config import ENTRY_MQTT_FALLBACK_ENABLED as fb
        from blocks.stop.breach_config import BREACH_CONFIRM_OBSERVATIONS as conf
        from blocks.stop.breach_config import BREACH_FILL_GRACE_SEC as grace
        from blocks.stop.reconcile_policy import (
            STOP_RECONCILE_OPEN_JITTER_SEC,
            STOP_RECONCILE_OPEN_SEC,
        )
        assert legacy_republish_enabled() is False
        assert fb is True
        assert grace == 10.0
        assert conf == 2
        assert STOP_RECONCILE_OPEN_SEC == 15.0
        assert STOP_RECONCILE_OPEN_JITTER_SEC == 5.0
