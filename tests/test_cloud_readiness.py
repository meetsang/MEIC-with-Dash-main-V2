"""Tests for Cloud Readiness, VIX1D streaming/recording, NYSE Early-Close Half-Days, SMS alerts, and Log Pruning."""
from __future__ import annotations

import os
import sys
import tempfile
import time as time_mod
from datetime import date, datetime, time
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


def test_vix1d_and_vix_watch_symbols_and_dxlink_mapping():
    from common.market_watch import (
        BASE_WATCH_SYMBOLS,
        DXLINK_QUOTE_SYMBOL,
        DXLINK_TO_MQTT,
        INDEX_TRADE_PRICE_SYMBOLS,
        dxlink_trade_symbols,
    )
    from market_data.watch_symbols import watch_symbol_from_mqtt_topic

    assert 'VIX1D' in BASE_WATCH_SYMBOLS
    assert 'VIX' in BASE_WATCH_SYMBOLS
    assert DXLINK_QUOTE_SYMBOL['VIX'] == 'VIX'
    assert DXLINK_QUOTE_SYMBOL['VIX1D'] == 'VIX1D'
    assert DXLINK_QUOTE_SYMBOL['VXN'] == 'VXN'
    assert 'VIX1D' in INDEX_TRADE_PRICE_SYMBOLS
    assert 'VIX' in INDEX_TRADE_PRICE_SYMBOLS
    assert 'SPX' in INDEX_TRADE_PRICE_SYMBOLS
    assert DXLINK_TO_MQTT['VIX1D'] == 'VIX1D'
    assert DXLINK_TO_MQTT['$VIX1D'] == 'VIX1D'
    assert watch_symbol_from_mqtt_topic('VIX1D') == 'VIX1D'
    assert watch_symbol_from_mqtt_topic('$VIX1D') == 'VIX1D'
    sub_syms = dxlink_trade_symbols()
    assert 'VIX1D' in sub_syms
    assert 'VIX' in sub_syms


def test_nyse_early_close_half_day_schedule():
    from common.config import _get_nyse_early_closes
    from common.market_hours import (
        early_close_shift_minutes,
        effective_session_time_ct,
        effective_session_time_str_ct,
        is_early_close_day_ct,
        market_close_time_ct,
    )
    from common.runtime_session import MEIC_SPX_0DTE, runtime_should_stop_for_session

    early_2026 = _get_nyse_early_closes(2026)
    # Day after Thanksgiving (Black Friday 2026-11-27) and Christmas Eve (2026-12-24) close at 12:00 CT (13:00 ET)
    assert early_2026.get('261127') == '12:00'
    assert early_2026.get('261224') == '12:00'

    black_friday = date(2026, 11, 27)
    normal_friday = date(2026, 11, 6)

    assert is_early_close_day_ct(black_friday) is True
    assert is_early_close_day_ct(normal_friday) is False

    assert market_close_time_ct(black_friday) == time(12, 0)
    assert market_close_time_ct(normal_friday) == time(15, 0)

    assert early_close_shift_minutes(black_friday) == 180
    assert early_close_shift_minutes(normal_friday) == 0

    # Afternoon cutoffs (14:50 EOD flatten, 14:51 Phase 3 proximity, 14:45 Mean Reversion) shift 3h earlier
    assert effective_session_time_ct('14:50', black_friday) == time(11, 50)
    assert effective_session_time_str_ct('14:50', black_friday) == '11:50'
    assert effective_session_time_ct('14:50', normal_friday) == time(14, 50)
    assert effective_session_time_ct(time(14, 51), black_friday) == time(11, 51)
    assert effective_session_time_ct('14:45', black_friday) == time(11, 45)
    # Morning times (< 11:00 CT) are unchanged
    assert effective_session_time_ct('08:30', black_friday) == time(8, 30)

    # MEIC runtime stops at 12:00 CT on Black Friday
    session_start = datetime(2026, 11, 27, 8, 30, 0)
    assert runtime_should_stop_for_session(session_start, datetime(2026, 11, 27, 11, 59, 0), profile=MEIC_SPX_0DTE) is False
    assert runtime_should_stop_for_session(session_start, datetime(2026, 11, 27, 12, 0, 0), profile=MEIC_SPX_0DTE) is True


def test_drma_hm_half_day_and_weekend_phases():
    from strategies.drma_hm.config import DRMAHMConfig
    from strategies.drma_hm.strategy import DRMAHMStrategy

    strat = DRMAHMStrategy(config=DRMAHMConfig())

    # Black Friday (2026-11-27): EOD flatten at 11:50 CT, market close / DAILY_HALT at 12:00 CT
    assert strat.get_session_phase(datetime(2026, 11, 27, 11, 45)) == 'REGULAR_TRADING'
    assert strat.get_session_phase(datetime(2026, 11, 27, 11, 50)) == 'EOD_FLATTEN'
    assert strat.get_session_phase(datetime(2026, 11, 27, 12, 0)) == 'DAILY_HALT'
    assert strat._is_eod_cutoff(datetime(2026, 11, 27, 11, 49)) is False
    assert strat._is_eod_cutoff(datetime(2026, 11, 27, 11, 50)) is True

    # Normal Friday (2026-11-06): EOD flatten at 14:50 CT, DAILY_HALT at 15:00 CT (and stays halted Friday evening)
    assert strat.get_session_phase(datetime(2026, 11, 6, 14, 45)) == 'REGULAR_TRADING'
    assert strat.get_session_phase(datetime(2026, 11, 6, 14, 50)) == 'EOD_FLATTEN'
    assert strat.get_session_phase(datetime(2026, 11, 6, 15, 0)) == 'DAILY_HALT'
    assert strat.get_session_phase(datetime(2026, 11, 6, 18, 0)) == 'DAILY_HALT'

    # Saturday (2026-11-07): DAILY_HALT all day
    assert strat.get_session_phase(datetime(2026, 11, 7, 10, 0)) == 'DAILY_HALT'
    assert strat.get_session_phase(datetime(2026, 11, 7, 20, 0)) == 'DAILY_HALT'

    # Sunday (2026-11-08): DAILY_HALT before 17:00 CT, OVERNIGHT_TRADING at/after 17:00 CT
    assert strat.get_session_phase(datetime(2026, 11, 8, 16, 30)) == 'DAILY_HALT'
    assert strat.get_session_phase(datetime(2026, 11, 8, 17, 0)) == 'OVERNIGHT_TRADING'


def test_drma_stocks_half_day_eod_resolution():
    from strategies.drma_stocks.config import DRMAStocksConfig
    from strategies.drma_stocks.strategy import DRMAStocksStrategy

    strat = DRMAStocksStrategy(config=DRMAStocksConfig())
    assert strat._resolve_eod_exit_time(for_date=date(2026, 11, 6)) == '14:50'
    assert strat._resolve_eod_exit_time(for_date=date(2026, 11, 27)) == '11:50'


def test_prune_old_session_logs_preserves_market_data():
    from common.session_cleanup import prune_old_session_logs

    with tempfile.TemporaryDirectory() as tmp:
        logs_dir = os.path.join(tmp, 'logs')
        data_dir = os.path.join(tmp, 'data', '2026-10-01')
        os.makedirs(logs_dir)
        os.makedirs(data_dir)

        old_log = os.path.join(logs_dir, 'launcher_2026-10-01_082000.log')
        recent_log = os.path.join(logs_dir, 'launcher_2026-11-07_082000.log')
        spx_csv = os.path.join(data_dir, 'SPX_1m.csv')
        vix1d_csv = os.path.join(data_dir, 'VIX1D_1m.csv')

        for p in (old_log, recent_log, spx_csv, vix1d_csv):
            with open(p, 'w', encoding='utf-8') as f:
                f.write('test\n')

        # Backdate old_log and data CSVs by 30 days
        old_ts = time_mod.time() - (30 * 86400)
        os.utime(old_log, (old_ts, old_ts))
        os.utime(spx_csv, (old_ts, old_ts))
        os.utime(vix1d_csv, (old_ts, old_ts))

        removed = prune_old_session_logs(tmp, keep_days=14)
        assert removed == 1
        assert not os.path.exists(old_log)
        assert os.path.exists(recent_log)
        # Market data CSVs in data/ are NEVER deleted regardless of age
        assert os.path.exists(spx_csv)
        assert os.path.exists(vix1d_csv)


def test_morning_readiness_check_and_sms_dispatch():
    from common.notifications import run_morning_readiness_check

    with patch('common.tt_auth.verify_tastytrade_oauth', return_value=(True, 'OAuth session valid (acct 5WT00001)')):
        res = run_morning_readiness_check(ROOT, session_label='Test 08:20 Check')
        assert res['oauth_ok'] is True
        assert '5WT00001' in res['oauth_message']
        assert 'market_close_ct' in res
