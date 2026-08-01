"""Tests for streamer symbol registration."""
from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.streamer_symbols import register_option_symbols


def test_register_option_symbols_converts_tastytrade(monkeypatch, tmp_path):
    sym_file = tmp_path / 'optsymbols.json'
    sym_file.write_text(json.dumps({'SYMBOLS': ['SPX']}), encoding='utf-8')
    monkeypatch.setattr('meic0dte.app.config.STREAM_SYMBOLS', str(sym_file))

    captured = {}

    def fake_update(symbols, lot, log):
        captured['symbols'] = symbols
        captured['lot'] = lot
        return True

    monkeypatch.setattr('meic0dte.app.utilities.update_options_symbols', fake_update)

    ok = register_option_symbols(
        ['.SPXW260731C7450', '.SPXW260731C7475'],
        'SKYLINE_SPX',
        MagicMock(),
    )
    assert ok is True
    assert captured['lot'] == 'SKYLINE_SPX'
    assert 'SPXW  260731C07450000' in captured['symbols']
    assert 'SPXW  260731C07475000' in captured['symbols']
