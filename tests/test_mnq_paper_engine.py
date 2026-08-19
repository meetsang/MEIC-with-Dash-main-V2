from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from mnq_paper.config import Settings
from mnq_paper.engine import EngineError, PaperEngine, bracket_levels, hit_reason, parse_side
from mnq_paper.journal import Journal
from mnq_paper.models import LiveQuote, OpenTrade, round_tick
from mnq_paper.server import _extract_side, create_app


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        product_code='MNQ',
        rest_symbol='/MNQU6',
        stop_points=20.0,
        target_points=40.0,
        qty=1,
        tick=0.25,
        point_value=2.0,
        webhook_secret='test-secret',
        host='127.0.0.1',
        port=8788,
        ngrok_enabled=False,
        ngrok_authtoken='',
        data_dir=tmp_path,
    )


def _engine(tmp_path: Path) -> PaperEngine:
    settings = _settings(tmp_path)
    journal = Journal(settings.journal_path, settings.open_path)
    return PaperEngine(settings, journal)


def test_parse_side():
    assert parse_side('BUY') == 'long'
    assert parse_side('sell') == 'short'
    with pytest.raises(EngineError):
        parse_side('hold')


def test_bracket_levels_long_and_short():
    stop, target = bracket_levels('long', 29800.1, 20, 40, 0.25)
    assert stop == 29780.0
    assert target == 29840.0
    stop, target = bracket_levels('short', 29800.0, 20, 40, 0.25)
    assert stop == 29820.0
    assert target == 29760.0


def test_hit_reason_stop_wins_on_gap():
    trade = OpenTrade(
        id='x',
        symbol='/MNQU6',
        streamer_symbol='/MNQU26:XCME',
        side='long',
        qty=1,
        entry=29800,
        stop=29780,
        target=29840,
        stop_points=20,
        target_points=40,
        opened_at='t',
        entry_source='rest',
    )
    assert hit_reason(trade, 29810) is None
    assert hit_reason(trade, 29840) == 'target'
    assert hit_reason(trade, 29780) == 'stop'
    assert hit_reason(trade, 29700) == 'stop'


def test_long_target_is_success(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29800, last=29800, mid=29800, source='rest'))
    trade = eng.open_signal('buy')
    assert trade.side == 'long'
    assert trade.entry == 29800
    closed = eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=29840, source='trade'))
    assert closed is not None
    assert closed.success is True
    assert closed.outcome == 'win'
    assert closed.reason == 'target'
    assert closed.pnl_usd == 80.0
    assert eng.open is None
    rows = eng.journal.recent_closed()
    assert len(rows) == 1
    assert rows[0]['success'] is True


def test_short_stop_is_loss(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', bid=29800, last=29800, mid=29800, source='rest'))
    eng.open_signal('sell')
    closed = eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=29820, source='trade'))
    assert closed is not None
    assert closed.success is False
    assert closed.outcome == 'loss'
    assert closed.pnl_usd == -40.0


def test_rejects_second_open(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29800, last=29800, source='rest'))
    eng.open_signal('buy')
    with pytest.raises(EngineError, match='Already'):
        eng.open_signal('sell')


def test_flatten_records_pnl(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29800, last=29800, mid=29800, source='rest'))
    eng.open_signal('buy')
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=29810, mid=29810, source='trade'))
    closed = eng.flatten()
    assert closed.outcome == 'flattened'
    assert closed.success is False
    assert closed.pnl_points == 10
    assert closed.pnl_usd == 20.0


def test_resumes_open_trade(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29800, last=29800, source='rest'))
    first = eng.open_signal('buy')
    eng2 = _engine(tmp_path)
    assert eng2.open is not None
    assert eng2.open.id == first.id


def test_extract_side_payloads():
    assert _extract_side({'side': 'buy'}) == 'buy'
    assert _extract_side({'action': 'sell'}) == 'sell'
    assert _extract_side({'strategy': {'order': {'action': 'buy'}}}) == 'buy'
    assert _extract_side('short') == 'short'


def test_webhook_opens_paper_trade(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29805.25, bid=29804.5, last=29805, source='rest'))
    app = create_app(eng)
    client = app.test_client()
    denied = client.post('/signal', json={'side': 'buy'})
    assert denied.status_code == 401
    ok = client.post('/signal', json={'side': 'buy'}, headers={'X-Webhook-Secret': 'test-secret'})
    assert ok.status_code == 200
    body = ok.get_json()
    assert body['trade']['side'] == 'long'
    assert body['trade']['entry'] == 29805.25
    assert body['trade']['paper'] is True


def test_round_tick():
    assert round_tick(29800.1, 0.25) == 29800.0
    assert round_tick(29800.13, 0.25) == 29800.25
