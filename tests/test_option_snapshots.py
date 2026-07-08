"""Tests for option quote snapshot recording."""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.stream_option_symbols import load_registered_option_symbols
from market_data import config
from market_data.option_snapshots import OptionQuoteSnapshotWriter


def test_load_registered_option_symbols_filters_indices(monkeypatch, tmp_path):
    sym_file = tmp_path / 'optsymbols.json'
    sym_file.write_text(
        json.dumps({
            'SYMBOLS': [
                'SPX',
                'SPXW  260707P07485000',
                'SPXW  260707P07460000',
            ],
        }),
        encoding='utf-8',
    )
    monkeypatch.setattr('common.stream_option_symbols.stream_config.STREAM_SYMBOLS', str(sym_file))
    syms = load_registered_option_symbols()
    assert len(syms) == 2
    assert all(s.startswith('.SPXW') for s in syms)


def test_option_snapshot_writes_all_symbols_once(monkeypatch, tmp_path):
    sym_file = tmp_path / 'optsymbols.json'
    sym_file.write_text(
        json.dumps({'SYMBOLS': ['SPXW  260707P07485000', 'SPXW  260707P07460000']}),
        encoding='utf-8',
    )
    monkeypatch.setattr('common.stream_option_symbols.stream_config.STREAM_SYMBOLS', str(sym_file))

    cache = MagicMock()
    cache.get_market_mid.side_effect = lambda s: {
        '.SPXW260707P7485': 1.25,
        '.SPXW260707P7460': 0.40,
    }.get(s)

    writer = OptionQuoteSnapshotWriter(cache)
    day_path = str(tmp_path / '2026-07-07')
    now = datetime(2026, 7, 7, 13, 49, 0)

    assert writer.maybe_write(now, day_path=day_path) is True
    assert writer.maybe_write(now, day_path=day_path) is False

    path = config.options_quotes_path(day_path)
    with open(path, encoding='utf-8', newline='') as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2
    assert {r['symbol'] for r in rows} == {'.SPXW260707P7460', '.SPXW260707P7485'}
    assert all(r['snapshot_ts'] == '2026-07-07 13:49:00' for r in rows)


def test_option_snapshot_skips_when_no_mqtt(monkeypatch, tmp_path):
    sym_file = tmp_path / 'optsymbols.json'
    sym_file.write_text(json.dumps({'SYMBOLS': ['SPXW  260707P07485000']}), encoding='utf-8')
    monkeypatch.setattr('common.stream_option_symbols.stream_config.STREAM_SYMBOLS', str(sym_file))

    cache = MagicMock()
    cache.get_market_mid.return_value = None

    writer = OptionQuoteSnapshotWriter(cache)
    day_path = str(tmp_path / '2026-07-07')
    assert writer.maybe_write(datetime(2026, 7, 7, 10, 0, 0), day_path=day_path) is False
