"""Tests for SPX ladder, sidecar collection, and subscribe caps."""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.market_watch import (
    SPX_LADDER_MAX_ACTIVE_SYMBOLS,
    sidecar_option_collection_enabled,
)
from common.stream_ladder_symbols import LADDER_SYMBOL_FILE, load_ladder_option_symbols
from market_data import config
from market_data.spx_ladder import (
    SpxLadderWriter,
    anchor_strike,
    ladder_option_symbols,
    strikes_for_spx,
)
from market_data.spx_ladder_snapshots import SpxLadderSnapshotWriter
from streaming.ladder_subscribe import build_quote_subscribe_set


def test_strike_grid_7533():
    assert anchor_strike(7533) == 7535
    strikes = strikes_for_spx(7533)
    assert len(strikes) == 100
    assert 7535 in strikes
    assert 7530 in strikes
    assert 7540 in strikes
    symbols = ladder_option_symbols(7533, '260709')
    assert len(symbols) == 200
    assert '.SPXW260709C7535' in symbols
    assert '.SPXW260709P7535' in symbols


def test_sidecar_enabled_by_default(monkeypatch):
    monkeypatch.delenv('MEIC_SIDE_OPTION_COLLECTION', raising=False)
    assert sidecar_option_collection_enabled() is True


def test_sidecar_disabled_by_env(monkeypatch):
    monkeypatch.setenv('MEIC_SIDE_OPTION_COLLECTION', '0')
    assert sidecar_option_collection_enabled() is False


def test_ladder_writer_generates_json(monkeypatch, tmp_path):
    monkeypatch.delenv('MEIC_SIDE_OPTION_COLLECTION', raising=False)
    monkeypatch.setattr('market_data.spx_ladder.LADDER_SYMBOL_FILE', str(tmp_path / 'ladder.json'))
    monkeypatch.setattr(
        'market_data.spx_ladder.is_ladder_session',
        lambda _now: True,
    )
    cache = MagicMock()
    cache.get_market_mid.return_value = 7533.0
    writer = SpxLadderWriter()
    assert writer.maybe_refresh(cache, now=datetime(2026, 7, 9, 10, 0, 0)) is True
    data = json.loads((tmp_path / 'ladder.json').read_text(encoding='utf-8'))
    assert data['anchor_strike'] == 7535
    assert len(data['SYMBOLS']) == 200


def test_sidecar_off_skips_ladder_json(monkeypatch, tmp_path):
    monkeypatch.setenv('MEIC_SIDE_OPTION_COLLECTION', '0')
    monkeypatch.setattr('market_data.spx_ladder.LADDER_SYMBOL_FILE', str(tmp_path / 'ladder.json'))
    monkeypatch.setattr('market_data.spx_ladder.is_ladder_session', lambda _now: True)
    cache = MagicMock()
    cache.get_market_mid.return_value = 7533.0
    writer = SpxLadderWriter()
    assert writer.maybe_refresh(cache, now=datetime(2026, 7, 9, 10, 0, 0)) is False
    assert not (tmp_path / 'ladder.json').exists()


def test_ladder_snapshot_three_columns_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv('MEIC_SIDE_OPTION_COLLECTION', raising=False)
    ladder_file = tmp_path / 'ladder.json'
    ladder_file.write_text(
        json.dumps({'SYMBOLS': ['.SPXW260709C7535', '.SPXW260709P7535']}),
        encoding='utf-8',
    )
    monkeypatch.setattr('common.stream_ladder_symbols.LADDER_SYMBOL_FILE', str(ladder_file))
    monkeypatch.setattr('market_data.spx_ladder_snapshots.is_ladder_session', lambda _now: True)

    cache = MagicMock()
    cache.get_market_mid.side_effect = lambda s: 1.25 if 'C' in s else 0.5

    day_path = str(tmp_path / '2026-07-09')
    writer = SpxLadderSnapshotWriter(cache)
    assert writer.maybe_write(datetime(2026, 7, 9, 10, 0, 0), day_path=day_path) is True

    path = config.spx_ladder_quotes_path(day_path)
    with open(path, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))
    assert list(rows[0].keys()) == ['snapshot_ts', 'strike', 'side', 'symbol', 'mid']
    assert len(rows) == 2


def test_sidecar_off_skips_ladder_csv(monkeypatch, tmp_path):
    monkeypatch.setenv('MEIC_SIDE_OPTION_COLLECTION', '0')
    cache = MagicMock()
    day_path = str(tmp_path / '2026-07-09')
    writer = SpxLadderSnapshotWriter(cache)
    assert writer.maybe_write(datetime(2026, 7, 9, 10, 0, 0), day_path=day_path) is False


def test_options_quotes_header_unchanged():
    from market_data.option_snapshots import _CSV_HEADER

    assert _CSV_HEADER == ('snapshot_ts', 'symbol', 'mid')


def test_subscribe_union_dedupes_trade_and_ladder(monkeypatch, tmp_path):
    monkeypatch.delenv('MEIC_SIDE_OPTION_COLLECTION', raising=False)
    opts = tmp_path / 'optsymbols.json'
    opts.write_text(
        json.dumps({'SYMBOLS': ['SPXW  260709C7535']}),
        encoding='utf-8',
    )
    ladder = tmp_path / 'ladder.json'
    ladder.write_text(
        json.dumps({'SYMBOLS': ['.SPXW260709C7535', '.SPXW260709P7535']}),
        encoding='utf-8',
    )
    monkeypatch.setattr('streaming.ladder_subscribe.config.STREAM_SYMBOLS', str(opts))
    monkeypatch.setattr('common.stream_ladder_symbols.LADDER_SYMBOL_FILE', str(ladder))

    quote_set, meta = build_quote_subscribe_set()
    assert '.SPXW260709C7535' in quote_set
    assert meta['trade_leg_count'] >= 1
    assert meta['ladder_count'] >= 1


def test_ladder_cap_prevents_runaway(monkeypatch, tmp_path):
    monkeypatch.delenv('MEIC_SIDE_OPTION_COLLECTION', raising=False)
    monkeypatch.setattr('streaming.ladder_subscribe.SPX_LADDER_MAX_ACTIVE_SYMBOLS', 3)
    opts = tmp_path / 'optsymbols.json'
    opts.write_text(json.dumps({'SYMBOLS': []}), encoding='utf-8')
    ladder_syms = [f'.SPXW260709C{7500 + i}' for i in range(10)]
    ladder = tmp_path / 'ladder.json'
    ladder.write_text(json.dumps({'SYMBOLS': ladder_syms}), encoding='utf-8')
    monkeypatch.setattr('streaming.ladder_subscribe.config.STREAM_SYMBOLS', str(opts))
    monkeypatch.setattr('common.stream_ladder_symbols.LADDER_SYMBOL_FILE', str(ladder))

    quote_set, meta = build_quote_subscribe_set()
    assert meta['ladder_cap_hit'] is True
    assert meta['ladder_count'] == 3


def test_load_ladder_empty_when_disabled(monkeypatch):
    monkeypatch.setenv('MEIC_SIDE_OPTION_COLLECTION', '0')
    assert load_ladder_option_symbols() == []
