"""Market data fetching, 5m bar caching, and indicator computation for DRMA Stocks."""
from __future__ import annotations

import datetime
import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from strategies.drma_hm.indicators import IndicatorSnapshot, compute_indicators
from strategies.drma_stocks.config import DRMAStocksConfig

log = logging.getLogger(__name__)

_CT = ZoneInfo('America/Chicago')
_ET = ZoneInfo('America/New_York')

_DEFAULT_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.5',
}

_DAILY_LEVELS_CACHE: Dict[Tuple[str, date], Dict[str, Optional[float]]] = {}
_LAST_DAILY_LEVELS_ATTEMPT: Dict[str, float] = {}
_LAST_5M_BARS_ATTEMPT: Dict[str, float] = {}


def _find_prev_trading_day_dir(
    data_root: str,
    current_date: date,
    filename: str,
) -> Optional[Tuple[date, str]]:
    """Scan local data directory to find the most recent trading date prior to current_date."""
    if not os.path.isdir(data_root):
        return None
    for offset in range(1, 10):
        prev_d = current_date - timedelta(days=offset)
        candidate = os.path.join(data_root, prev_d.isoformat(), filename)
        if os.path.isfile(candidate):
            return prev_d, candidate
    return None


def get_nearest_weekly_expiry(for_date: Optional[date] = None, min_dte: int = 0, max_dte: int = 7) -> str:
    """Find the nearest Friday options expiration within [min_dte, max_dte].
    
    Returns 6-digit YYMMDD string (e.g. '261030').
    """
    today = for_date or datetime.datetime.now(_ET).date()
    # Check next 14 days for a Friday (weekday == 4)
    for d_offset in range(min_dte, max_dte + 8):
        candidate = today + timedelta(days=d_offset)
        if candidate.weekday() == 4 and (candidate - today).days >= min_dte:
            return candidate.strftime('%y%m%d')
    # Fallback to upcoming Friday
    days_ahead = (4 - today.weekday()) % 7
    if days_ahead == 0 and min_dte > 0:
        days_ahead = 7
    return (today + timedelta(days=days_ahead)).strftime('%y%m%d')


def fetch_stock_daily_levels(ticker: str, for_date: date, root: str = '') -> Dict[str, Optional[float]]:
    """Fetch previous day High (PDH), Low (PDL), and Close (PDC).
    
    Checks memory cache first, then locally recorded previous-day 5m CSV.
    Only queries Yahoo Finance as an initial cold-start fallback (cached for the day).
    """
    clean_ticker = (ticker or '').strip().upper()
    cache_key = (clean_ticker, for_date)
    cached = _DAILY_LEVELS_CACHE.get(cache_key)
    if cached and cached.get('pdc') is not None:
        return cached

    levels: Dict[str, Optional[float]] = {'pdh': None, 'pdl': None, 'pdc': None, 'onh': None, 'onl': None}

    # 1. Check local recorded data from previous day (PDH/PDL/PDC) and today's pre-market (ONH/ONL)
    base = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    data_root = os.path.join(base, 'data')
    prev_info = _find_prev_trading_day_dir(data_root, for_date, f'{clean_ticker}_5m.csv')
    if prev_info:
        _, prev_csv = prev_info
        try:
            df_prev = pd.read_csv(prev_csv, on_bad_lines='skip')
            if not df_prev.empty and 'close' in df_prev.columns and 'high' in df_prev.columns and 'low' in df_prev.columns:
                levels['pdh'] = round(float(df_prev['high'].max()), 2)
                levels['pdl'] = round(float(df_prev['low'].min()), 2)
                levels['pdc'] = round(float(df_prev.iloc[-1]['close']), 2)
        except Exception as exc:
            log.debug("Could not read prev day %s for %s levels: %s", prev_csv, clean_ticker, exc)

    # Check today's pre-market bars (before 08:30 CT) for stock overnight/pre-market high & low (ONH/ONL)
    day_csv = os.path.join(data_root, for_date.isoformat(), f'{clean_ticker}_5m.csv')
    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
            if not df_today.empty and 'datetime' in df_today.columns and 'high' in df_today.columns and 'low' in df_today.columns:
                pm_mask = df_today['datetime'].astype(str).str.slice(11, 19) < '08:30:00'
                df_pm = df_today[pm_mask]
                if not df_pm.empty:
                    levels['onh'] = round(float(df_pm['high'].max()), 2)
                    levels['onl'] = round(float(df_pm['low'].min()), 2)
        except Exception as exc:
            log.debug("Could not read pre-market bars for %s: %s", clean_ticker, exc)

    if levels.get('pdc') is not None:
        _DAILY_LEVELS_CACHE[cache_key] = levels
        log.info("Loaded daily levels for %s from local files: %s", clean_ticker, levels)
        return levels

    # 2. Cooldown check before querying Yahoo Finance (max once every 60s per ticker)
    now_mono = time.monotonic()
    last_att = _LAST_DAILY_LEVELS_ATTEMPT.get(clean_ticker, 0.0)
    if (now_mono - last_att) < 60.0 and clean_ticker in _LAST_DAILY_LEVELS_ATTEMPT:
        return _DAILY_LEVELS_CACHE.get(cache_key, levels)
    _LAST_DAILY_LEVELS_ATTEMPT[clean_ticker] = now_mono

    # 3. Fetch from Yahoo Finance as fallback
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{clean_ticker}?range=7d&interval=1d'
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=8)
        if resp.status_code == 200:
            res = resp.json().get('chart', {}).get('result', [])
            if res:
                data = res[0]
                timestamps = data.get('timestamp', [])
                quote = data.get('indicators', {}).get('quote', [{}])[0]
                highs = quote.get('high', [])
                lows = quote.get('low', [])
                closes = quote.get('close', [])

                records = []
                for ts, h, l, c in zip(timestamps, highs, lows, closes):
                    if ts is None or c is None:
                        continue
                    bar_date = datetime.datetime.fromtimestamp(ts, tz=_ET).date()
                    if bar_date < for_date:
                        records.append((bar_date, float(h or c), float(l or c), float(c)))

                if records:
                    last_day = records[-1]
                    levels['pdh'] = round(last_day[1], 2)
                    levels['pdl'] = round(last_day[2], 2)
                    levels['pdc'] = round(last_day[3], 2)
                    _DAILY_LEVELS_CACHE[cache_key] = levels
                    return levels
    except Exception as exc:
        log.warning("Could not fetch daily levels for %s: %s", clean_ticker, exc)

    if levels.get('pdc') is not None:
        _DAILY_LEVELS_CACHE[cache_key] = levels
    return levels


def _load_local_stock_5m_bars(
    ticker: str,
    for_date: date,
    root: str = '',
    prepend_prev_bars: int = 40,
) -> pd.DataFrame:
    """Load 5m OHLC bars for ticker from local TastyTrade recorded CSV files."""
    clean_ticker = (ticker or '').strip().upper()
    base = root or os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    data_root = os.path.join(base, 'data')

    fname = f'{clean_ticker}_5m.csv'
    day_csv = os.path.join(data_root, for_date.isoformat(), fname)
    prev_info = _find_prev_trading_day_dir(data_root, for_date, filename=fname)

    df_today = pd.DataFrame()
    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
        except Exception as exc:
            log.debug("Could not read today %s: %s", day_csv, exc)

    df_prev = pd.DataFrame()
    if prev_info:
        _, prev_csv_path = prev_info
        try:
            df_prev = pd.read_csv(prev_csv_path, on_bad_lines='skip')
        except Exception as exc:
            log.debug("Could not read prev day %s: %s", prev_csv_path, exc)

    if df_today.empty and df_prev.empty:
        return pd.DataFrame()

    req_cols = ['datetime', 'open', 'high', 'low', 'close']
    frames = []

    # Warmup bars from previous trading day
    if not df_prev.empty and 'datetime' in df_prev.columns:
        for c in req_cols:
            if c not in df_prev.columns:
                df_prev[c] = 0.0
        p_sub = df_prev[req_cols].dropna().tail(max(0, prepend_prev_bars)).copy()
        p_sub['is_today'] = False
        frames.append(p_sub)

    # Today's bars
    if not df_today.empty and 'datetime' in df_today.columns:
        for c in req_cols:
            if c not in df_today.columns:
                df_today[c] = 0.0
        t_sub = df_today[req_cols].dropna().copy()
        t_sub['is_today'] = True
        frames.append(t_sub)

    if not frames:
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.drop_duplicates(subset=['datetime'], keep='last')
    combined = combined.sort_values('datetime').reset_index(drop=True)

    # Check polls.csv for latest live streamer tick to keep active candle current
    polls_path = os.path.join(data_root, for_date.isoformat(), f'{clean_ticker}_polls.csv')
    if os.path.isfile(polls_path):
        try:
            with open(polls_path, 'rb') as f:
                f.seek(max(0, os.path.getsize(polls_path) - 1024))
                lines = f.read().decode('utf-8', errors='ignore').strip().splitlines()
                if len(lines) >= 2:
                    last_line = lines[-1].split(',')
                    if len(last_line) >= 2:
                        live_price = float(last_line[1])
                        if live_price > 0 and not combined.empty:
                            # Update latest close & high/low with live streamer mid
                            last_idx = combined.index[-1]
                            combined.at[last_idx, 'close'] = live_price
                            combined.at[last_idx, 'high'] = max(combined.at[last_idx, 'high'], live_price)
                            combined.at[last_idx, 'low'] = min(combined.at[last_idx, 'low'], live_price)
        except Exception:
            pass

    return combined


def fetch_stock_5m_bars(ticker: str, for_date: date, root: str = '') -> pd.DataFrame:
    """Fetch 5m bars for an individual stock.
    
    Loads from locally recorded TastyTrade streamer CSV files first.
    Falls back to Yahoo Finance only if local CSV data is not yet available.
    """
    clean_ticker = (ticker or '').strip().upper()

    # 1. Try local recorded TastyTrade streamer bars
    df_local = _load_local_stock_5m_bars(clean_ticker, for_date, root=root)
    if not df_local.empty and len(df_local) >= 2:
        return df_local

    # 2. Cooldown check for Yahoo Finance fallback (max once every 30s per ticker)
    now_mono = time.monotonic()
    last_att = _LAST_5M_BARS_ATTEMPT.get(clean_ticker, 0.0)
    if (now_mono - last_att) < 30.0 and clean_ticker in _LAST_5M_BARS_ATTEMPT:
        return df_local if not df_local.empty else pd.DataFrame()
    _LAST_5M_BARS_ATTEMPT[clean_ticker] = now_mono

    # 3. Fallback to Yahoo Finance
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{clean_ticker}?range=5d&interval=5m'
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=8)
        if resp.status_code != 200:
            return df_local if not df_local.empty else pd.DataFrame()
        res = resp.json().get('chart', {}).get('result', [])
        if not res:
            return df_local if not df_local.empty else pd.DataFrame()
        data = res[0]
        timestamps = data.get('timestamp', [])
        quote = data.get('indicators', {}).get('quote', [{}])[0]
        opens = quote.get('open', [])
        highs = quote.get('high', [])
        lows = quote.get('low', [])
        closes = quote.get('close', [])

        all_bars = []
        for t, o, h, l, c in zip(timestamps, opens, highs, lows, closes):
            if c is None or o is None or h is None or l is None:
                continue
            dt_ct = datetime.datetime.fromtimestamp(t, tz=_CT)
            all_bars.append({
                'datetime': dt_ct.strftime('%Y-%m-%d %H:%M:%S'),
                'open': round(float(o), 2),
                'high': round(float(h), 2),
                'low': round(float(l), 2),
                'close': round(float(c), 2),
                'date': dt_ct.date(),
            })

        if not all_bars:
            return df_local if not df_local.empty else pd.DataFrame()

        df_all = pd.DataFrame(all_bars)
        # Select target date or fallback to most recent date available in data (e.g. weekends)
        available_dates = sorted(df_all['date'].unique())
        target = for_date if for_date in available_dates else (available_dates[-1] if available_dates else for_date)
        
        # Include up to 40 preceding bars for smooth EMA initialization
        target_idx = df_all[df_all['date'] == target].index
        if len(target_idx) == 0:
            return df_local if not df_local.empty else pd.DataFrame()
        first_target_pos = target_idx[0]
        start_pos = max(0, first_target_pos - 40)
        df_sub = df_all.iloc[start_pos:].copy()
        df_sub['is_today'] = (df_sub['date'] == target)
        return df_sub.drop(columns=['date']).reset_index(drop=True)
    except Exception as exc:
        log.warning("Could not fetch 5m bars for %s: %s", clean_ticker, exc)
        return df_local if not df_local.empty else pd.DataFrame()


def compute_stock_indicators(
    ticker: str,
    df_bars: pd.DataFrame,
    levels: Dict[str, Optional[float]],
    cfg: DRMAStocksConfig,
) -> Tuple[pd.DataFrame, List[IndicatorSnapshot]]:
    """Compute DRMA 21 EMA, ORB 30m, and Hilega-Milega momentum matrix for a stock."""
    if df_bars.empty or 'close' not in df_bars.columns:
        return df_bars, []

    current_price = float(df_bars.iloc[-1]['close'])
    # Scale slope threshold and extension points relative to stock price
    min_slope = round(max(0.01, current_price * cfg.min_slope_pct), 3)
    max_extension = round(max(0.50, current_price * cfg.max_extension_pct), 2)

    df_calc = df_bars.copy()
    if 'datetime' not in df_calc.columns and 'timestamp' in df_calc.columns:
        df_calc['datetime'] = pd.to_datetime(df_calc['timestamp'])
    if 'is_today' not in df_calc.columns:
        df_calc['is_today'] = True

    df_out, snapshots = compute_indicators(
        df_calc,
        ma_type=cfg.ma_type,
        ma_len=cfg.ma_len,
        orb_minutes=cfg.orb_minutes,
        min_slope_pts=min_slope,
        max_extension_pts=max_extension,
        pdh=levels.get('pdh'),
        pdl=levels.get('pdl'),
        pdc=levels.get('pdc'),
        onh=levels.get('onh'),
        onl=levels.get('onl'),
    )
    return df_out, snapshots


def select_stock_options_strikes(
    ticker: str,
    side: str,                  # 'C' or 'P'
    spot: float,
    spread_type: str,           # 'debit_spread' or 'credit_spread'
    spread_width: int = 10,
    expiry_yymmdd: str = '',
) -> Tuple[int, int, str, str]:
    """Calculate long and short strike prices for a 10-wide spread on a stock.
    
    Returns (long_strike, short_strike, long_symbol, short_symbol).
    """
    from common.symbols import build_tastytrade_symbol

    # Round spot to nearest integer dollar
    atm = int(round(spot))

    if spread_type == 'debit_spread':
        # Directional Debit Spread
        if side.upper() == 'C':
            # Bull Call Spread: Long lower strike (ATM), Short higher strike (+10)
            long_k = atm
            short_k = atm + spread_width
        else:
            # Bear Put Spread: Long higher strike (ATM), Short lower strike (-10)
            long_k = atm
            short_k = max(1, atm - spread_width)
    else:
        # Non-Directional Credit Spread (OTM)
        otm_offset = max(2, int(round(spot * 0.03)))  # ~3% OTM
        if side.upper() == 'P':
            # Bull Put Spread (Credit): Short higher OTM put, Long lower put (-10)
            short_k = max(spread_width + 1, atm - otm_offset)
            long_k = max(1, short_k - spread_width)
        else:
            # Bear Call Spread (Credit): Short lower OTM call, Long higher call (+10)
            short_k = atm + otm_offset
            long_k = short_k + spread_width

    long_sym = build_tastytrade_symbol(expiry_yymmdd, side.upper(), long_k, ticker=ticker) if expiry_yymmdd else f"{ticker}_{side}{long_k}"
    short_sym = build_tastytrade_symbol(expiry_yymmdd, side.upper(), short_k, ticker=ticker) if expiry_yymmdd else f"{ticker}_{side}{short_k}"

    return long_k, short_k, long_sym, short_sym
