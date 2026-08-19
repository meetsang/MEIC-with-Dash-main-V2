"""Tests for manual 1-2-1 butterfly helpers and scanner."""
from __future__ import annotations

from blocks.entry.butterfly_scan import _iter_body_strikes, _iter_body_strikes_from_chain, scan_butterflies
from manual_spread.butterfly import (
    BUY_CALL_BF,
    SELL_CALL_BF,
    build_butterfly_legs,
    is_butterfly_side,
    net_butterfly_mid,
    order_price_for_butterfly,
    strikes_for_body,
)


def test_strikes_for_body_5_wide():
    wl, body, wh = strikes_for_body(7505, 5)
    assert (wl, body, wh) == (7500, 7505, 7510)


def test_iter_body_strikes_from_chain_call_fly():
    from common.chain_snapshot import ChainSnapshot

    expiry = '260731'
    mids = {}
    deltas = {}
    for strike in range(7490, 7545, 5):
        sym = f'.SPXW{expiry}C{strike}'
        mids[sym] = 1.0
        if strike <= 7500:
            deltas[sym] = 0.55
        elif strike <= 7510:
            deltas[sym] = 0.40
        else:
            deltas[sym] = 0.20

    snap = ChainSnapshot(
        expiry=expiry,
        captured_at=0,
        spx_price=7900,
        otm_range=300,
        mids=mids,
        deltas=deltas,
        anchor_strike=7500,
    )
    bodies, _incomplete = _iter_body_strikes_from_chain(
        snap, expiry, 'C', wing_half=5, otm_min=5, otm_max=30,
    )
    assert 7765 not in bodies  # not in chain
    assert 7510 in bodies
    assert 7520 in bodies
    assert 7505 not in bodies  # wing_low 7500 not above ref 7500


def test_call_bodies_only_above_spx():
    spx = 5503.0
    bodies = _iter_body_strikes(spx, 'C', wing_half=5, otm_min=5, otm_max=25)
    assert bodies
    for body in bodies:
        wl, _, _ = strikes_for_body(body, 5)
        assert wl > spx
        assert body - spx >= 5


def test_put_bodies_only_below_spx():
    spx = 5503.0
    bodies = _iter_body_strikes(spx, 'P', wing_half=5, otm_min=5, otm_max=25)
    assert bodies
    for body in bodies:
        _, _, wh = strikes_for_body(body, 5)
        assert wh < spx
        assert spx - body >= 5


def test_sell_call_butterfly_legs_121():
    legs = build_butterfly_legs('260731', 'C', 7500, 7505, 7510, 1, sell=True)
    assert len(legs) == 3
    assert legs[0].action == 'SELL_TO_OPEN' and legs[0].quantity == 1
    assert legs[1].action == 'BUY_TO_OPEN' and legs[1].quantity == 2
    assert legs[2].action == 'SELL_TO_OPEN' and legs[2].quantity == 1


def test_buy_call_butterfly_legs_121():
    legs = build_butterfly_legs('260731', 'C', 7500, 7505, 7510, 1, sell=False)
    assert legs[0].action == 'BUY_TO_OPEN'
    assert legs[1].action == 'SELL_TO_OPEN' and legs[1].quantity == 2
    assert legs[2].action == 'BUY_TO_OPEN'


def test_net_butterfly_mid_sell_credit():
    credit = net_butterfly_mid(0.50, 2.00, 0.50, sell=True)
    assert credit == 3.0


def test_net_butterfly_mid_buy_debit():
    debit = net_butterfly_mid(2.00, 0.50, 2.00, sell=False)
    assert debit == 3.0


def test_order_price_sign():
    assert order_price_for_butterfly(0.50, sell=True) == 0.50
    assert order_price_for_butterfly(4.50, sell=False) == -4.50


def test_is_butterfly_side():
    assert is_butterfly_side(SELL_CALL_BF)
    assert is_butterfly_side(BUY_CALL_BF)
    assert not is_butterfly_side('P')


def test_scan_butterflies_ranks_by_target(monkeypatch):
    class FakeBroker:
        def fetch_spx_price_api(self):
            return 7500.0

        def get_spx_price(self):
            return 7500.0

    mids = {}

    def fake_fetch(_broker, symbols, _log, **_kwargs):
        out = {}
        for sym in symbols:
            from common.symbols import parse_canonical

            parsed = parse_canonical(sym)
            strike = parsed[2] if parsed else 0
            out[sym] = mids.get(strike, 1.0)
            from common.symbols import to_tastytrade

            out[to_tastytrade(sym)] = mids.get(strike, 1.0)
        return out

    monkeypatch.setattr('blocks.entry.butterfly_scan._fetch_option_mids_robust', fake_fetch)

    mids.update({7510: 0.40, 7515: 1.00, 7520: 0.40, 7525: 0.35, 7530: 0.30})
    results, diag = scan_butterflies(
        FakeBroker(),
        structure_type='sell_call_bf',
        expiry='260731',
        wing_half=5,
        target_price=1.20,
        max_results=3,
        otm_min=5,
        otm_max=30,
    )
    assert diag.bodies_scanned > 0
    assert results
    assert results[0].distance_from_target == min(c.distance_from_target for c in results)
