"""Unit tests for manual spread scan without otm_min or otm_max (searching full downloaded chain)."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from flask import Flask

from common.chain_snapshot import (
    ChainSnapshot,
    clear_snapshot,
    _snapshots,
)
from common.symbols import to_tastytrade
from dashboard.manual_spread_handlers import register_manual_spread_routes
from manual_spread import entry as ms_entry
from tests.mock_broker import MockBroker

EXPIRY = '260808'
EXPIRY_YYMMDD = '260808'
LOG = logging.getLogger('test_manual_spread_scan')


def _setup_mock_chain(spx: int = 5900, band: int = 100):
    clear_snapshot()
    mids = {}
    deltas = {}
    for strike in range(spx - band, spx + band + 5, 5):
        for opt in ('C', 'P'):
            sym = f'.SPXW{EXPIRY_YYMMDD}{opt}{strike}'
            dist = abs(spx - strike)
            mids[sym] = max(0.10, 5.0 - dist * 0.04)
            deltas[sym] = 0.50 if opt == 'C' else -0.50

    snap = ChainSnapshot(
        expiry=EXPIRY_YYMMDD,
        captured_at=0,
        spx_price=spx,
        otm_range=band,
        mids=mids,
        deltas=deltas,
        anchor_strike=spx,
    )
    _snapshots[EXPIRY_YYMMDD] = snap
    return snap


def test_scan_spreads_vertical_defaults_to_downloaded_chain_band():
    _setup_mock_chain(spx=5900, band=120)
    broker = MockBroker()
    broker.prices['SPX'] = 5900.0

    res = ms_entry.scan_spreads(
        broker,
        side='P',
        expiry=EXPIRY,
        spread_width=25,
        target_credit=0.60,
        otm_min=None,
        otm_max=None,
    )
    assert res['status'] == 'ok'
    assert res['otm_min'] == 5
    assert res['otm_max'] == 120
    assert res['diagnostics']['otm_min'] == 5
    assert res['diagnostics']['otm_max'] == 120
    assert res['diagnostics']['strikes_scanned'] == (120 - 5) // 5 + 1


def test_scan_spreads_vertical_respects_explicit_otm_range():
    _setup_mock_chain(spx=5900, band=120)
    broker = MockBroker()
    broker.prices['SPX'] = 5900.0

    res = ms_entry.scan_spreads(
        broker,
        side='P',
        expiry=EXPIRY,
        spread_width=25,
        target_credit=0.60,
        otm_min=10,
        otm_max=40,
    )
    assert res['status'] == 'ok'
    assert res['otm_min'] == 10
    assert res['otm_max'] == 40
    assert res['diagnostics']['otm_min'] == 10
    assert res['diagnostics']['otm_max'] == 40


def test_scan_spreads_butterfly_defaults_to_downloaded_chain_band():
    _setup_mock_chain(spx=5900, band=100)
    broker = MockBroker()

    res = ms_entry.scan_spreads(
        broker,
        side='C',
        expiry=EXPIRY,
        spread_width=5,
        target_credit=0.50,
        structure_type='sell_call_bf',
        otm_min=None,
        otm_max=None,
    )
    assert res['status'] == 'ok'
    assert res['otm_min'] == 5
    assert res['otm_max'] == 100


def test_scan_spreads_butterfly_respects_explicit_otm_max():
    _setup_mock_chain(spx=5900, band=100)
    broker = MockBroker()

    res = ms_entry.scan_spreads(
        broker,
        side='C',
        expiry=EXPIRY,
        spread_width=5,
        target_credit=0.50,
        structure_type='sell_call_bf',
        otm_min=10,
        otm_max=25,
    )
    assert res['status'] == 'ok'
    assert res['otm_min'] == 10
    assert res['otm_max'] == 25


def test_api_scan_endpoint_without_otm_min_or_max():
    _setup_mock_chain(spx=5900, band=150)
    broker = MockBroker()
    broker.prices['SPX'] = 5900.0

    app = Flask(__name__)
    register_manual_spread_routes(
        app,
        live_prices={},
        index_topic="TASTYTRADE/SPX",
        trades_dir="/tmp/trades",
        central_now_str=lambda: "14:00:00 CT",
        tranche_lots=[],
        tranche_sides=[],
        launcher_active_fn=lambda: True,
    )
    client = app.test_client()

    with patch('dashboard.manual_spread_handlers.get_shared_broker', return_value=broker):
        # Post scan payload with chain_band but without otm_min or otm_max (matches updated UI)
        res = client.post(
            '/api/manual_spread/scan',
            json={
                'underlying': 'SPX',
                'expiry': EXPIRY,
                'spread_width': 25,
                'target_credit': 0.60,
                'quantity': 1,
                'chain_band': 150,
                'side': 'P',
            },
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'ok'
        assert data['otm_min'] == 5
        assert data['otm_max'] == 150


def test_api_load_and_scan_endpoint():
    clear_snapshot()
    broker = MockBroker()
    broker.prices['SPX'] = 5900.0
    broker.fetch_spx_price_api = MagicMock(return_value=5900.0)

    def fake_mids(symbols):
        out = {}
        for s in symbols:
            tt = to_tastytrade(s)
            # symbol format: SPXW  260808P05900000 or .SPXW260808P5900
            digits = ''.join(filter(str.isdigit, tt))
            strike = int(digits[-4:]) if len(digits) >= 4 else 5900
            dist = abs(5900 - strike)
            out[tt] = max(0.10, 5.0 - dist * 0.04)
        return out

    broker.fetch_option_mids_api = MagicMock(side_effect=fake_mids)

    app = Flask(__name__)
    register_manual_spread_routes(
        app,
        live_prices={},
        index_topic="TASTYTRADE/SPX",
        trades_dir="/tmp/trades",
        central_now_str=lambda: "14:00:00 CT",
        tranche_lots=[],
        tranche_sides=[],
        launcher_active_fn=lambda: True,
    )
    client = app.test_client()

    with patch('dashboard.manual_spread_handlers.get_shared_broker', return_value=broker):
        res = client.post(
            '/api/manual_spread/load_and_scan',
            json={
                'underlying': 'SPX',
                'expiry': EXPIRY,
                'chain_band': 100,
                'spread_width': 25,
                'target_credit': 0.60,
                'quantity': 1,
                'side': 'P',
            },
        )
        assert res.status_code == 200
        data = res.get_json()
        assert data['status'] == 'ok'
        assert 'chain' in data
        assert data['chain']['chain_band'] == 100
        assert data['chain']['symbol_count'] > 0
        assert data['otm_min'] == 5
        assert data['otm_max'] == 100
        assert len(data['candidates']) > 0
