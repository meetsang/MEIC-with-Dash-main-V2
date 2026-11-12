"""Unit tests for DRMA Stocks multi-stock strategy."""

import json
import os
import shutil
import tempfile
import pytest
from datetime import date, datetime, timezone

from strategies.drma_stocks.config import DRMAStocksConfig
from strategies.drma_stocks.watchlist import (
    load_watchlist, save_watchlist, add_ticker, remove_ticker, toggle_ticker, DEFAULT_WATCHLIST
)
from strategies.drma_stocks.market_data import (
    get_nearest_weekly_expiry, select_stock_options_strikes, compute_stock_indicators
)
from strategies.drma_stocks.strategy import DRMAStocksStrategy
from common.history_adapter import is_drma_stocks_trade, drma_stocks_trade_to_history_row
from common import trades_layout


@pytest.fixture
def temp_root():
    d = tempfile.mkdtemp(prefix="test_drma_stocks_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_watchlist_operations(temp_root):
    # Test initial default loading
    wl = load_watchlist(temp_root)
    assert len(wl) == 5
    symbols = [item['symbol'] for item in wl]
    assert symbols == ['NVDA', 'MU', 'AMZN', 'SPCX', 'MSTR']

    # Test adding a new ticker
    ok, msg = add_ticker("TSLA", name="Tesla Inc.", root=temp_root)
    assert ok is True
    wl_after_add = load_watchlist(temp_root)
    assert len(wl_after_add) == 6
    assert any(item['symbol'] == 'TSLA' for item in wl_after_add)

    # Test adding duplicate ticker
    ok_dup, msg_dup = add_ticker("TSLA", root=temp_root)
    assert ok_dup is False
    assert "already in watchlist" in msg_dup

    # Test toggling active/paused
    ok_tog, msg_tog = toggle_ticker("TSLA", enabled=False, root=temp_root)
    assert ok_tog is True
    wl_after_tog = load_watchlist(temp_root)
    tsla_item = next(item for item in wl_after_tog if item['symbol'] == 'TSLA')
    assert tsla_item['enabled'] is False

    # Test removing a ticker
    ok_rem, msg_rem = remove_ticker("TSLA", root=temp_root)
    assert ok_rem is True
    wl_after_rem = load_watchlist(temp_root)
    assert len(wl_after_rem) == 5
    assert not any(item['symbol'] == 'TSLA' for item in wl_after_rem)


def test_config_defaults():
    cfg = DRMAStocksConfig()
    assert cfg.paper is True
    assert cfg.spread_width == 10
    assert cfg.min_dte == 0
    assert cfg.max_dte == 7
    assert cfg.equity_allocation_dollars == 5000.0
    assert cfg.ma_len == 21
    assert cfg.orb_minutes == 30
    assert cfg.market_open_time_cst == '08:30'
    assert cfg.regular_trading_start_time_cst == '09:00'
    assert cfg.eod_exit_time_cst == '14:50'
    assert cfg.force_eod_exit is True


def test_nearest_weekly_expiry():
    # Thursday Oct 22 -> Nearest Friday is Oct 23 (1 DTE)
    ref = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
    exp = get_nearest_weekly_expiry(ref, min_dte=0, max_dte=7)
    assert exp == "261023"

    # Friday Oct 23 -> Nearest Friday is Oct 23 (0 DTE)
    ref_fri = datetime(2026, 10, 23, 10, 0, tzinfo=timezone.utc)
    exp_fri = get_nearest_weekly_expiry(ref_fri, min_dte=0, max_dte=7)
    assert exp_fri == "261023"


def test_select_stock_options_strikes_bull_call():
    long_k, short_k, long_sym, short_sym = select_stock_options_strikes(
        ticker="NVDA", side="C", spot=142.50, spread_type="debit_spread", spread_width=10
    )
    assert long_k == 142
    assert short_k == 152
    assert "NVDA" in long_sym
    assert "NVDA" in short_sym


def test_select_stock_options_strikes_bear_put():
    long_k, short_k, long_sym, short_sym = select_stock_options_strikes(
        ticker="NVDA", side="P", spot=142.50, spread_type="debit_spread", spread_width=10
    )
    assert long_k == 142
    assert short_k == 132
    assert "NVDA" in long_sym
    assert "NVDA" in short_sym


def test_compute_stock_indicators_without_onh_onl():
    import pandas as pd
    bars = []
    base_price = 100.0
    for i in range(40):
        price = base_price + i * 0.5
        bars.append({
            'datetime': f"2026-10-26 08:{30 + (i%6)*5:02d}:00",
            'open': price - 0.2,
            'high': price + 0.6,
            'low': price - 0.3,
            'close': price,
            'volume': 1000 + i * 10,
            'is_today': True
        })
    df_bars = pd.DataFrame(bars)
    daily_levels = {'pdh': 115.0, 'pdl': 95.0, 'pdc': 98.0}
    cfg = DRMAStocksConfig()
    df_out, snapshots = compute_stock_indicators('NVDA', df_bars, levels=daily_levels, cfg=cfg)
    assert not df_out.empty
    assert len(snapshots) > 0
    snap = snapshots[-1]
    assert snap.active_score >= 0
    assert any(g in snap.setup_grade.upper() for g in ('GRADE A+', 'GRADE A', 'GRADE B', 'GRADE C', 'NO SETUP'))


def test_direct_equity_sizing():
    # With $5,000 allocation:
    # Spot = $142.50 -> 5000 / 142.5 = 35 shares
    shares_nvda = max(1, int(5000.0 / 142.50))
    assert shares_nvda == 35

    # Spot = $450.00 -> 5000 / 450 = 11 shares
    shares_high = max(1, int(5000.0 / 450.0))
    assert shares_high == 11

    # Spot = $6000.00 -> max(1, 0) = 1 share minimum
    shares_ultra = max(1, int(5000.0 / 6000.0))
    assert shares_ultra == 1


def test_history_adapter_drma_stocks():
    trade = {
        'trade_id': 'DRMA_STK_NVDA_20261026_093000',
        'strategy': trades_layout.STRATEGY_DRMA_STOCKS,
        'sub_strategy': trades_layout.STRATEGY_DRMA_STOCK_DIR,
        'ticker': 'NVDA',
        'status': 'closed',
        'paper': True,
        'date': '2026-10-26',
        'entered_at': '2026-10-26T09:30:00-05:00',
        'closed_at': '2026-10-26T11:45:00-05:00',
        'side': 'BULL',
        'entry_price': 3.50,
        'exit_price': 5.25,
        'realized_pnl_dollars': 175.0,
        'structures': [
            {
                'name': 'Bull Call Spread 10W',
                'legs': [{'strike': 140.0}, {'strike': 150.0}]
            }
        ]
    }

    assert is_drma_stocks_trade(trade) is True
    row = drma_stocks_trade_to_history_row(trade)
    assert row is not None
    assert row['strategy'] == trades_layout.STRATEGY_DRMA_STOCKS
    assert row['ticker'] == 'NVDA'
    assert row['pnl'] == 175.0
    assert row['paper'] == 1
    assert row['status'] == 'CLOSED'
    assert row['short_strike'] == '150.0'
    assert row['long_strike'] == '140.0'


def test_strategy_evaluation_creates_trades(temp_root):
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    class DummySnap:
        bias = 'BULL'
        active_score = 5
        setup_grade = 'Grade A+'

    # 1. Test direct stock trade creation ($5K buying power = 35 shares @ $142.50)
    strat._create_direct_stock_trade(
        ticker='NVDA',
        snap=DummySnap(),
        spot=142.50,
        active_dir=active_dir,
    )
    files = [f for f in os.listdir(active_dir) if f.endswith('.json')]
    assert len(files) == 1
    with open(os.path.join(active_dir, files[0])) as f:
        t = json.load(f)
    assert t['ticker'] == 'NVDA'
    assert t['status'] == 'open'
    assert t['sub_strategy'] == 'DRMA_STOCK_EQUITY'
    assert t['shares'] == 35
    assert t['paper'] is True

    # 2. Test 10-wide option spread creation
    strat._create_option_spread_trade(
        ticker='NVDA',
        snap=DummySnap(),
        spot=142.50,
        expiry='2026-10-30',
        spread_type='debit_spread',
        side='C',
        active_dir=active_dir,
    )
    files2 = [f for f in os.listdir(active_dir) if f.endswith('.json')]
    assert len(files2) == 2
    spread_file = next(f for f in files2 if '_equity_' not in f)
    with open(os.path.join(active_dir, spread_file)) as f:
        sp = json.load(f)
    assert sp['ticker'] == 'NVDA'
    assert sp['sub_strategy'] == 'DRMA_STOCK_DIR'
    assert sp['status'] == 'open'
    assert sp['structures'][0]['legs'][0]['strike'] == 142
    assert sp['structures'][0]['legs'][1]['strike'] == 152
    assert sp['structures'][0]['legs'][1]['strike'] - sp['structures'][0]['legs'][0]['strike'] == 10


def test_drma_stocks_dashboard_apis():
    from dashboard.server import app
    app.config['TESTING'] = True
    with app.test_client() as client:
        # 1. Test GET /api/drma_stocks/today
        res_today = client.get('/api/drma_stocks/today')
        assert res_today.status_code == 200
        data_today = res_today.get_json()
        assert data_today['strategy'] == 'DRMA_HM_STOCKS'
        assert data_today['paper'] is True
        assert 'watchlist' in data_today
        assert 'stocks' in data_today
        assert 'active_trades' in data_today
        assert 'history_trades' in data_today
        assert 'today_pnl' in data_today

        # 2. Test GET /api/drma_stocks/watchlist
        res_wl = client.get('/api/drma_stocks/watchlist')
        assert res_wl.status_code == 200
        data_wl = res_wl.get_json()
        assert 'tickers' in data_wl
        symbols = [t['symbol'] for t in data_wl['tickers']]
        assert all(s in symbols for s in ['NVDA', 'MU', 'AMZN', 'SPCX', 'MSTR'])

        # 3. Test POST /api/drma_stocks/watchlist (add, toggle, remove)
        res_add = client.post(
            '/api/drma_stocks/watchlist',
            data=json.dumps({'action': 'add', 'symbol': 'AMD', 'name': 'Advanced Micro Devices'}),
            content_type='application/json'
        )
        assert res_add.status_code == 200
        assert res_add.get_json()['status'] == 'ok'

        res_tog = client.post(
            '/api/drma_stocks/watchlist',
            data=json.dumps({'action': 'toggle', 'symbol': 'AMD', 'enabled': False}),
            content_type='application/json'
        )
        assert res_tog.status_code == 200
        assert res_tog.get_json()['status'] == 'ok'

        res_rem = client.post(
            '/api/drma_stocks/watchlist',
            data=json.dumps({'action': 'remove', 'symbol': 'AMD'}),
            content_type='application/json'
        )
        assert res_rem.status_code == 200
        assert res_rem.get_json()['status'] == 'ok'

        # 4. Test GET /api/drma_stocks/history
        res_hist = client.get('/api/drma_stocks/history')
        assert res_hist.status_code == 200
        data_hist = res_hist.get_json()
        assert 'by_ticker' in data_hist
        assert 'by_sub_strategy' in data_hist
        assert 'total_trades' in data_hist


def test_drma_stocks_avoids_entry_before_orb_formed(temp_root):
    """DRMA stocks must avoid entries before the 30m ORB completes at 09:00 AM CST."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    class DummySnapEarly:
        bias = 'BULL'
        active_score = 5
        setup_grade = 'Grade A+'
        datetime_str = '2026-10-26 08:45:00'  # In 30m ORB formation window
        orb_formed = False                    # 30m ORB has not formed yet

    strat._check_entry_signal(
        ticker='NVDA',
        snap=DummySnapEarly(),
        spot=142.50,
        today=datetime(2026, 10, 26).date(),
        active_dir=active_dir,
        existing_trades=[],
    )

    # Must be 0 trades created because 30m ORB is still forming
    files = [f for f in os.listdir(active_dir) if f.endswith('.json')]
    assert len(files) == 0


def test_drma_stocks_permits_entry_after_orb_formed(temp_root):
    """DRMA stocks permits entry at/after 9:00 AM CST once 30m ORB is marked and formed."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    class DummySnapReady:
        bias = 'BULL'
        active_score = 5
        setup_grade = 'Grade A+'
        datetime_str = '2026-10-26 09:05:00'  # After 30m ORB (after 09:00 CST)
        orb_formed = True                     # 30m ORB formed

    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    # Mock CST clock to 09:05 CST to ensure clock-based checks also pass
    mock_now = datetime(2026, 10, 26, 9, 5, 0, tzinfo=ZoneInfo('America/Chicago'))
    with patch('strategies.drma_stocks.strategy.datetime') as mock_dt:
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        strat._check_entry_signal(
            ticker='NVDA',
            snap=DummySnapReady(),
            spot=142.50,
            today=datetime(2026, 10, 26).date(),
            active_dir=active_dir,
            existing_trades=[],
        )

    files = [f for f in os.listdir(active_dir) if f.endswith('.json')]
    assert len(files) > 0


def test_drma_stocks_force_eod_exit(temp_root):
    """DRMA stocks forces exit at 14:50 CST (end of active day cutoff)."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    trade_path = os.path.join(active_dir, 'NVDA_equity_long_123456.json')
    trade_data = {
        'trade_id': 'NVDA_equity_long_123456',
        'ticker': 'NVDA',
        'status': 'open',
        'sub_strategy': 'DRMA_STOCK_EQUITY',
        'structure_type': 'equity_stock',
        'side': 'LONG',
        'shares': 35,
        'entry_price': 142.50,
        'current_mark': 143.00,
        'profit_target_price': 146.77,
        'stop_loss_price': 139.65,
    }
    with open(trade_path, 'w', encoding='utf-8') as f:
        json.dump(trade_data, f)

    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    mock_eod = datetime(2026, 10, 26, 14, 55, 0, tzinfo=ZoneInfo('America/Chicago'))
    with patch('strategies.drma_stocks.strategy.datetime') as mock_dt:
        mock_dt.now.return_value = mock_eod
        strat._check_trade_exit(trade_data, trade_path, 143.00, hist_dir)

    assert trade_data['status'] == 'closed'
    assert 'EOD Cutoff' in trade_data.get('exit_reason', '')
    # File moved to history
    assert not os.path.exists(trade_path)
    assert os.path.exists(os.path.join(hist_dir, 'NVDA_equity_long_123456.json'))



def test_drma_stocks_flattens_prior_day_trade_immediately(temp_root):
    """DRMA stocks is day-trading only: trades carried over from a prior day must flatten immediately."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    trade_path = os.path.join(active_dir, 'NVDA_pcs_prior_day.json')
    # Entered 2 days ago on 2026-10-27
    trade_data = {
        'trade_id': 'NVDA_pcs_prior_day',
        'ticker': 'NVDA',
        'status': 'open',
        'sub_strategy': 'DRMA_STOCK_NONDIR',
        'structure_type': 'credit_spread',
        'side': 'LONG',
        'opt_type': 'P',
        'entry_price': 1.00,
        'current_mark': 0.80,
        'entered_at': '2026-10-27T09:48:12.384375',
        'profit_target_price': 0.50,
        'stop_loss_price': 2.00,
    }
    with open(trade_path, 'w', encoding='utf-8') as f:
        json.dump(trade_data, f)

    from unittest.mock import patch
    from zoneinfo import ZoneInfo
    # Evaluated at 09:30 AM CST on 2026-10-29 (2 days later during normal market hours)
    mock_morning = datetime(2026, 10, 29, 9, 30, 0, tzinfo=ZoneInfo('America/Chicago'))
    with patch('strategies.drma_stocks.strategy.datetime') as mock_dt:
        mock_dt.now.return_value = mock_morning
        strat._check_trade_exit(trade_data, trade_path, 142.50, hist_dir, today=date(2026, 10, 29))

    assert trade_data['status'] == 'closed'
    assert 'EOD Cutoff' in trade_data.get('exit_reason', '')
    assert not os.path.exists(trade_path)
    assert os.path.exists(os.path.join(hist_dir, 'NVDA_pcs_prior_day.json'))


def test_drma_stocks_aligns_with_drma_hm_flatten_override(temp_root):
    """DRMA stocks must align with DRMA_HM_SPX flatten time override."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    cfg = DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=temp_root)
    strat = DRMAStocksStrategy(config=cfg)

    # By default, 14:50
    assert strat._resolve_eod_exit_time(temp_root) == '14:50'

    # Set override on DRMA_HM_SPX to 14:40
    from common.daily_overrides import set_overrides
    from strategies.drma_hm.config import DRMAHMConfig
    set_overrides('DRMA_HM_SPX', temp_root, {'eod_exit_time_cst': '14:40'}, DRMAHMConfig)

    # DRMA stocks automatically aligns with DRMA_HM_SPX flatten time
    assert strat._resolve_eod_exit_time(temp_root) == '14:40'


def test_drma_stocks_settle_open_stock_trade(temp_root):
    """settle_open_stock_trade flattens open stock trades during EOD session cleanup."""
    trades_layout.ensure_all_trade_dirs(temp_root)
    from strategies.drma_stocks.settlement import settle_open_stock_trade
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, temp_root)

    trade_path = os.path.join(active_dir, 'MU_equity_cleanup_test.json')
    trade_data = {
        'trade_id': 'MU_equity_cleanup_test',
        'ticker': 'MU',
        'strategy': 'DRMA_HM_STOCKS',
        'status': 'open',
        'structure_type': 'equity_stock',
        'side': 'LONG',
        'shares': 50,
        'entry_price': 100.0,
        'current_spot': 102.0,
    }
    with open(trade_path, 'w', encoding='utf-8') as f:
        json.dump(trade_data, f)

    res = settle_open_stock_trade(trade_path, temp_root, for_date=date(2026, 10, 30))
    assert res is True
    assert not os.path.exists(trade_path)
    hist_file = os.path.join(hist_dir, 'MU_equity_cleanup_test.json')
    assert os.path.exists(hist_file)
    with open(hist_file, 'r', encoding='utf-8') as f:
        saved = json.load(f)
    assert saved['status'] == 'closed'
    assert 'EOD Cutoff' in saved['exit_reason']
    assert saved['realized_pnl_dollars'] == 100.0

def test_drma_stocks_local_5m_bars_and_daily_levels(temp_root):
    """DRMA stocks loads 5m bars and daily levels from local TastyTrade recorded CSV files without Yahoo."""
    from strategies.drma_stocks.market_data import fetch_stock_5m_bars, fetch_stock_daily_levels
    import pandas as pd

    data_dir_prev = os.path.join(temp_root, 'data', '2026-10-29')
    data_dir_today = os.path.join(temp_root, 'data', '2026-10-30')
    os.makedirs(data_dir_prev, exist_ok=True)
    os.makedirs(data_dir_today, exist_ok=True)

    # Previous day CSV
    df_prev = pd.DataFrame([
        {'datetime': '2026-10-29 08:35:00', 'open': 140.0, 'high': 143.0, 'low': 139.5, 'close': 142.0},
        {'datetime': '2026-10-29 14:55:00', 'open': 142.0, 'high': 144.5, 'low': 141.5, 'close': 144.0},
    ])
    df_prev.to_csv(os.path.join(data_dir_prev, 'NVDA_5m.csv'), index=False)

    # Today CSV
    df_today = pd.DataFrame([
        {'datetime': '2026-10-30 08:35:00', 'open': 144.5, 'high': 146.0, 'low': 144.0, 'close': 145.5},
        {'datetime': '2026-10-30 08:40:00', 'open': 145.5, 'high': 147.0, 'low': 145.0, 'close': 146.5},
    ])
    df_today.to_csv(os.path.join(data_dir_today, 'NVDA_5m.csv'), index=False)

    # Test daily levels from local previous day CSV
    levels = fetch_stock_daily_levels('NVDA', date(2026, 10, 30), root=temp_root)
    assert levels['pdh'] == 144.5
    assert levels['pdl'] == 139.5
    assert levels['pdc'] == 144.0

    # Test 5m bars loaded from local CSV
    df_bars = fetch_stock_5m_bars('NVDA', date(2026, 10, 30), root=temp_root)
    assert not df_bars.empty
    assert len(df_bars) == 4
    today_bars = df_bars[df_bars['is_today'] == True]
    prev_bars = df_bars[df_bars['is_today'] == False]
    assert len(today_bars) == 2
    assert len(prev_bars) == 2
    assert float(today_bars.iloc[-1]['close']) == 146.5


def test_drma_stocks_watch_universe_inclusion():
    """DRMA stock symbols must be included in WATCH_SYMBOLS and map canonical MQTT topics."""
    from common.market_watch import WATCH_SYMBOLS, get_drma_stock_watch_symbols
    from market_data.watch_symbols import watch_symbol_from_mqtt_topic

    drma_syms = get_drma_stock_watch_symbols()
    for s in ('NVDA', 'MU', 'AMZN', 'SPCX', 'MSTR'):
        assert s in drma_syms
        assert s in WATCH_SYMBOLS
        assert watch_symbol_from_mqtt_topic(f'TASTYTRADE/{s}') == s
        assert watch_symbol_from_mqtt_topic(f'TASTYTRADE/{s}__TSIZE') == s


def test_manual_spread_exempt_from_pre_tranche_probe(temp_root):
    """Manual spread entries must not be blocked by missing pre_tranche records when REST is healthy."""
    from common.trading_gate import initialize_for_session_date, evaluate_new_risk_gate, record_probe_result
    from common.rest_probe import RestProbeResult
    import time

    os.environ['MEIC_TRADING_GATE_PATH'] = os.path.join(temp_root, 'trading_gate.json')
    initialize_for_session_date('2026-11-06')

    # Record healthy probe
    now = time.time()
    probe_res = RestProbeResult(
        ok=True,
        status='healthy',
        attempted_at_epoch=now - 5,
        completed_at_epoch=now,
        latency_ms=120,
        http_status=200,
        detail='',
        source='startup',
    )
    record_probe_result(probe_res)

    # For manual slot key 'ms-714_P', evaluate gate: must NOT block with 'rest_probe_missing'
    decision = evaluate_new_risk_gate(
        require_fresh_probe=True,
        strategy='MANUAL_SPREAD',
        tranche_id='ms-714_P',
    )
    assert decision.blocked is False
