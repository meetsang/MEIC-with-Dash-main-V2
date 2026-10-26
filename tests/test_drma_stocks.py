"""Unit tests for DRMA Stocks multi-stock strategy."""

import json
import os
import shutil
import tempfile
import pytest
from datetime import datetime, timezone

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

