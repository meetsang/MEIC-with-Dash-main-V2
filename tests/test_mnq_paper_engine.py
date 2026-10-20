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
from mnq_paper.server import _extract_side_from_payload, create_app
from mnq_paper.signals import extract_action, extract_quantity, extract_strategy, normalize_payload


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        product_code='MNQ',
        rest_symbol='/MNQU6',
        stop_points=40.0,
        target_points=25.0,
        qty=1,
        tick=0.25,
        point_value=2.0,
        webhook_secret='test-secret',
        host='127.0.0.1',
        port=8788,
        ngrok_enabled=False,
        ngrok_authtoken='',
        allowed_strategy='apexnq',
        data_dir=tmp_path,
    )


def _engine(tmp_path: Path) -> PaperEngine:
    settings = _settings(tmp_path)
    journal = Journal(settings.journal_path, settings.open_path)
    return PaperEngine(settings, journal)


def test_parse_side():
    assert parse_side('BUY') == 'long'
    assert parse_side('buy') == 'long'
    assert parse_side('bull') == 'long'
    assert parse_side('bullish') == 'long'
    assert parse_side('long') == 'long'
    assert parse_side('b') == 'long'

    assert parse_side('SELL') == 'short'
    assert parse_side('sell') == 'short'
    assert parse_side('bear') == 'short'
    assert parse_side('bearish') == 'short'
    assert parse_side('short') == 'short'
    assert parse_side('s') == 'short'

    with pytest.raises(EngineError):
        parse_side('hold')


def test_bracket_levels_40_stop_25_target():
    # Long: Stop = Entry - 40, Target = Entry + 25
    stop, target = bracket_levels('long', 20000.0, 40.0, 25.0, 0.25)
    assert stop == 19960.0
    assert target == 20025.0

    # Short: Stop = Entry + 40, Target = Entry - 25
    stop, target = bracket_levels('short', 20000.0, 40.0, 25.0, 0.25)
    assert stop == 20040.0
    assert target == 19975.0


def test_hit_reason_stop_and_target():
    trade_long = OpenTrade(
        id='x1',
        symbol='/MNQU6',
        streamer_symbol='/MNQU26:XCME',
        side='long',
        qty=1,
        entry=20000.0,
        stop=19960.0,
        target=20025.0,
        stop_points=40.0,
        target_points=25.0,
        signal_received_at='2026-08-21T12:00:00+00:00',
        opened_at='2026-08-21T12:00:01+00:00',
        entry_source='rest',
    )
    assert hit_reason(trade_long, 20010.0) is None
    assert hit_reason(trade_long, 20025.0) == 'target'
    assert hit_reason(trade_long, 20030.0) == 'target'
    assert hit_reason(trade_long, 19960.0) == 'stop'
    assert hit_reason(trade_long, 19950.0) == 'stop'

    trade_short = OpenTrade(
        id='x2',
        symbol='/MNQU6',
        streamer_symbol='/MNQU26:XCME',
        side='short',
        qty=1,
        entry=20000.0,
        stop=20040.0,
        target=19975.0,
        stop_points=40.0,
        target_points=25.0,
        signal_received_at='2026-08-21T12:00:00+00:00',
        opened_at='2026-08-21T12:00:01+00:00',
        entry_source='rest',
    )
    assert hit_reason(trade_short, 19990.0) is None
    assert hit_reason(trade_short, 19975.0) == 'target'
    assert hit_reason(trade_short, 19960.0) == 'target'
    assert hit_reason(trade_short, 20040.0) == 'stop'
    assert hit_reason(trade_short, 20050.0) == 'stop'


def test_long_target_is_success_with_timestamps(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, last=20000.0, mid=20000.0, source='rest'))
    trade = eng.open_signal('buy', signal_received_at='2026-08-21T12:00:00+00:00')
    assert trade.side == 'long'
    assert trade.entry == 20000.0
    assert trade.stop == 19960.0
    assert trade.target == 20025.0
    assert trade.signal_received_at == '2026-08-21T12:00:00+00:00'
    assert trade.opened_at is not None

    # Streamer price ticks up to 20025.0 (PT hit)
    closed = eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=20025.0, source='trade'))
    assert closed is not None
    assert closed.success is True
    assert closed.outcome == 'win'
    assert closed.reason == 'target'
    assert closed.pnl_points == 25.0
    assert closed.pnl_usd == 50.0  # 25 pts * 1 qty * $2
    assert closed.signal_received_at == '2026-08-21T12:00:00+00:00'
    assert closed.opened_at == trade.opened_at
    assert closed.closed_at is not None
    assert eng.open is None

    rows = eng.journal.recent_closed()
    assert len(rows) == 1
    assert rows[0]['success'] is True
    assert rows[0]['pnl_usd'] == 50.0
    assert rows[0]['signal_received_at'] == '2026-08-21T12:00:00+00:00'


def test_short_stop_is_loss_with_timestamps(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', bid=20000.0, last=20000.0, mid=20000.0, source='rest'))
    trade = eng.open_signal('sell', signal_received_at='2026-08-21T13:00:00+00:00')
    assert trade.side == 'short'
    assert trade.entry == 20000.0
    assert trade.stop == 20040.0
    assert trade.target == 19975.0

    # Streamer price ticks up to 20040.0 (SL hit)
    closed = eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=20040.0, source='trade'))
    assert closed is not None
    assert closed.success is False
    assert closed.outcome == 'loss'
    assert closed.reason == 'stop'
    assert closed.pnl_points == -40.0
    assert closed.pnl_usd == -80.0  # -40 pts * 1 qty * $2
    assert closed.signal_received_at == '2026-08-21T13:00:00+00:00'
    assert closed.closed_at is not None


def test_rejects_second_open(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=29800, last=29800, source='rest'))
    eng.open_signal('buy')
    with pytest.raises(EngineError, match='Already'):
        eng.open_signal('sell')


def test_flatten_records_pnl(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, last=20000.0, mid=20000.0, source='rest'))
    eng.open_signal('buy')
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', last=20010.0, mid=20010.0, source='trade'))
    closed = eng.flatten()
    assert closed.outcome == 'flattened'
    assert closed.success is False
    assert closed.pnl_points == 10.0
    assert closed.pnl_usd == 20.0


def test_resumes_open_trade(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, last=20000.0, source='rest'))
    first = eng.open_signal('buy')
    eng2 = _engine(tmp_path)
    assert eng2.open is not None
    assert eng2.open.id == first.id


def test_friend_bullish_payload_webhook(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, bid=19999.75, last=20000.0, source='rest'))
    app = create_app(eng)
    client = app.test_client()

    payload = {
        "aname": "pavi",
        "account": "Sim101",
        "ticker": "NQ1!",
        "action": "buy",
        "quantity": "1",
        "price": "{{close}}",
        "strategy": "apexnq",
    }

    res = client.post('/signal', json=payload, headers={'X-Webhook-Secret': 'test-secret'})
    assert res.status_code == 200
    data = res.get_json()
    assert data['ok'] is True
    trade = data['trade']
    assert trade['side'] == 'long'
    assert trade['entry'] == 20000.0
    assert trade['stop'] == 19960.0   # 40 points SL
    assert trade['target'] == 20025.0 # 25 points PT
    assert trade['qty'] == 1
    assert trade['signal']['aname'] == 'pavi'
    assert trade['signal']['account'] == 'Sim101'
    assert trade['signal']['strategy'] == 'apexnq'
    assert trade['signal_received_at'] is not None


def test_friend_bearish_payload_webhook(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.25, bid=20000.0, last=20000.0, source='rest'))
    app = create_app(eng)
    client = app.test_client()

    payload = {
        "aname": "pavi",
        "account": "Sim101",
        "ticker": "NQ1!",
        "action": "sell",
        "quantity": "1",
        "price": "{{close}}",
        "strategy": "apexnq",
    }

    res = client.post('/signal', json=payload, headers={'X-Webhook-Secret': 'test-secret'})
    assert res.status_code == 200
    data = res.get_json()
    assert data['ok'] is True
    trade = data['trade']
    assert trade['side'] == 'short'
    assert trade['entry'] == 20000.0
    assert trade['stop'] == 20040.0   # 40 points SL
    assert trade['target'] == 19975.0 # 25 points PT
    assert trade['qty'] == 1


def test_auth_options(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, bid=19999.75, last=20000.0, source='rest'))
    app = create_app(eng)
    client = app.test_client()

    payload = {"action": "buy", "strategy": "apexnq"}

    # 1. Missing secret -> 401
    assert client.post('/signal', json=payload).status_code == 401

    # 2. X-Webhook-Secret header -> 200
    res = client.post('/signal', json=payload, headers={'X-Webhook-Secret': 'test-secret'})
    assert res.status_code == 200
    eng.flatten(price=20000.0)

    # 3. Authorization Bearer -> 200
    res = client.post('/signal', json=payload, headers={'Authorization': 'Bearer test-secret'})
    assert res.status_code == 200
    eng.flatten(price=20000.0)

    # 4. Query param secret -> 200
    res = client.post('/signal?secret=test-secret', json=payload)
    assert res.status_code == 200
    eng.flatten(price=20000.0)

    # 5. Payload body secret -> 200
    res = client.post('/signal', json={"action": "buy", "strategy": "apexnq", "secret": "test-secret"})
    assert res.status_code == 200


def test_strategy_filter_rejection(tmp_path: Path):
    eng = _engine(tmp_path)
    eng.update_quote(LiveQuote('/MNQU6', '/MNQU26:XCME', ask=20000.0, source='rest'))
    app = create_app(eng)
    client = app.test_client()

    payload = {
        "action": "buy",
        "strategy": "other_strategy",
    }
    res = client.post('/signal', json=payload, headers={'X-Webhook-Secret': 'test-secret'})
    assert res.status_code == 400
    assert 'not allowed' in res.get_json()['error']


def test_round_tick():
    assert round_tick(29800.1, 0.25) == 29800.0
    assert round_tick(29800.13, 0.25) == 29800.25

