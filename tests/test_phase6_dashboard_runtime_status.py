"""Phase 6 dashboard runtime status display tests."""
from __future__ import annotations

from unittest.mock import patch

from blocks.stop import state as state_mod
from dashboard.runtime_display import (
    breach_readiness_label,
    decorate_entry_label,
    is_expired_trade,
    is_protective_estimate,
    quote_source_label,
)
from dashboard.server import _apply_trade_overlay, _slot_state_from_trade, app


def _trade(**overrides):
    st = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot='01-15_P',
        side='P',
        short_symbol='.SPXW260709P7530',
        long_symbol='.SPXW260709P7485',
        short_strike=7530,
        long_strike=7485,
        short_fill=1.15,
        long_fill=0.40,
        net_credit=0.75,
        quantity=1,
        open_order_id='1',
    )
    st.update(overrides)
    return st


def test_protective_estimate_entry_label():
    trade = _trade()
    trade['entry']['fill_confidence'] = 'protective_estimate'
    trade['open_order'] = {'fill_sync': {'phase': 'resolved_estimated'}}
    label = decorate_entry_label(trade, '0.75 (1.15-0.40)')
    assert label.startswith('Estimated Fill ·')


def test_expired_state_display():
    trade = _trade(status='closed', close_mechanism='expiry_settlement', settled_at_expiry=True)
    assert _slot_state_from_trade('closed', 'expiry_settlement', trade=trade) == 'expired'
    assert is_expired_trade(trade)


def test_quote_source_labels():
    assert quote_source_label({'entry': {'quote_source': 'rest'}}) == 'REST'
    assert quote_source_label({'entry_quote_source': 'mqtt_fallback'}) == 'MQTT fallback'


def test_breach_readiness_label_from_watch():
    trade = _trade(
        breach_watch={
            'software_breach_ready': False,
            'quote_pair_reason': 'fill_grace',
            'fill_grace_remaining_sec': 4.2,
        },
    )
    assert 'grace' in breach_readiness_label(trade).lower()


def test_apply_trade_overlay_includes_runtime_labels():
    trade = _trade(
        entry_quote_source='mqtt_fallback',
        breach_watch={'software_breach_ready': True},
    )
    slot = {}
    with patch('dashboard.server._live_price', return_value=None):
        _apply_trade_overlay(slot, trade, '01-15', 'P')
    assert slot['entry_quote_source_label'] == 'MQTT fallback'
    assert slot['breach_readiness_label'] == 'SW breach ready'


def test_api_summary_and_broker_health_no_broker_calls():
    client = app.test_client()
    with patch('dashboard.server._live_price', return_value=None), \
         patch('dashboard.server.build_manual_trades', return_value=([], 0, 0, 0)), \
         patch('dashboard.server._read_active_trades', return_value=[]), \
         patch('dashboard.server.bootstrap_meic_session_if_missing', return_value=None), \
         patch('common.broker_factory.get_shared_broker') as broker_get:
        resp = client.get('/api/summary')
        health = client.get('/api/broker_health')
    assert resp.status_code == 200
    assert health.status_code == 200
    broker_get.assert_not_called()
