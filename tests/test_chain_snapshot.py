"""Tests for SPX chain snapshot cache."""
from __future__ import annotations

import logging
import os
import sys
import time
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.chain_snapshot import (
    ChainSnapshot,
    ChainSnapshotNotLoaded,
    build_band_symbols,
    clear_snapshot,
    fetch_entry_mids_fresh,
    fetch_option_mids_with_snapshot,
    get_snapshot,
    lookup_mids_from_snapshot,
    require_loaded_chain,
    round_spx_strike,
    warm_chain_snapshot,
)
from common.symbols import build_tastytrade_symbol, to_tastytrade

log = logging.getLogger('test_chain_snapshot')
EXPIRY = '260808'


def test_round_spx_strike():
    assert round_spx_strike(5893) == 5895
    assert round_spx_strike(5892) == 5890


def test_build_band_symbols_count():
    symbols = build_band_symbols(EXPIRY, 5900, 25)
    # 11 strikes × 2 = 22
    assert len(symbols) == 22
    assert symbols[0] == build_tastytrade_symbol(EXPIRY, 'C', 5875)


def test_warm_and_reuse_snapshot():
    clear_snapshot()
    broker = MagicMock()
    broker.fetch_spx_price_api.return_value = 5900.0
    broker.get_spx_price.return_value = None

    def fake_fetch(symbols):
        return {to_tastytrade(s): 1.5 for s in symbols}

    broker.fetch_option_mids_api = fake_fetch

    snap = warm_chain_snapshot(broker, EXPIRY, log, otm_range=25)
    assert snap.spx_price == 5900
    assert snap.otm_range == 25
    assert len(snap.mids) == 22
    assert get_snapshot(EXPIRY) is snap

    broker.fetch_option_mids_api = MagicMock()
    need = [
        build_tastytrade_symbol(EXPIRY, 'P', 5880),
        build_tastytrade_symbol(EXPIRY, 'P', 5875),
    ]
    out = fetch_option_mids_with_snapshot(
        broker, need, EXPIRY, log, otm_max_needed=25, spx_price=5900,
    )
    assert len(out) == 2
    broker.fetch_option_mids_api.assert_not_called()


def test_snapshot_stays_valid_without_ttl():
    clear_snapshot()
    broker = MagicMock()

    def fake_fetch(symbols):
        return {to_tastytrade(s): 2.0 for s in symbols}

    broker.fetch_option_mids_api = fake_fetch
    warm_chain_snapshot(broker, EXPIRY, log, otm_range=10, spx_price=5900)
    snap = get_snapshot(EXPIRY)
    snap.captured_at = time.time() - 99999
    assert snap.is_fresh()
    require_loaded_chain(EXPIRY, 10)


def test_chain_snapshot_dataclass():
    snap = ChainSnapshot(expiry=EXPIRY, captured_at=time.time(), spx_price=5900, otm_range=50)
    assert snap.is_fresh()
    assert snap.is_fresh(ttl_sec=0)
    assert snap.covers_otm(40)
    assert not snap.covers_otm(60)


def test_require_loaded_chain_raises():
    clear_snapshot()
    try:
        require_loaded_chain(EXPIRY, 50)
        assert False, 'expected ChainSnapshotNotLoaded'
    except ChainSnapshotNotLoaded as exc:
        assert 'not loaded' in str(exc).lower()


def test_fetch_entry_mids_fresh_bypasses_stale_snapshot():
    clear_snapshot()
    broker = MagicMock()
    sym = build_tastytrade_symbol(EXPIRY, 'C', 5905)

    broker.fetch_option_mids_api = MagicMock(return_value={to_tastytrade(sym): 1.5})
    warm_chain_snapshot(broker, EXPIRY, log, otm_range=25, spx_price=5900)
    snap = get_snapshot(EXPIRY)
    snap.mids[to_tastytrade(sym)] = 99.0

    broker.fetch_option_mids_api = MagicMock(return_value={to_tastytrade(sym): 0.12})
    out = fetch_entry_mids_fresh(broker, [sym], EXPIRY, log)
    assert out[to_tastytrade(sym)] == 0.12
    assert snap.mids[to_tastytrade(sym)] == 0.12
    broker.fetch_option_mids_api.assert_called_once()


def test_lookup_mids_no_rest():
    clear_snapshot()
    broker = MagicMock()

    def fake_fetch(symbols):
        return {to_tastytrade(s): 1.5 for s in symbols}

    broker.fetch_option_mids_api = fake_fetch
    warm_chain_snapshot(
        broker, EXPIRY, log, otm_range=25, spx_price=5900,
    )
    broker.fetch_option_mids_api = MagicMock()
    need = [build_tastytrade_symbol(EXPIRY, 'P', 5880)]
    out = fetch_option_mids_with_snapshot(
        broker, need, EXPIRY, log, otm_max_needed=25, allow_warm=False,
    )
    assert len(out) == 1
    broker.fetch_option_mids_api.assert_not_called()
