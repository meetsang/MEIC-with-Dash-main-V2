"""Market data fetching, 5m bar caching, and indicator computation for DRMA Stocks."""
from __future__ import annotations

import datetime
import logging
import os
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


def fetch_stock_daily_levels(ticker: str, for_date: date) -> Dict[str, Optional[float]]:
    """Fetch previous day High (PDH), Low (PDL), and Close (PDC) from Yahoo Finance."""
    levels: Dict[str, Optional[float]] = {'pdh': None, 'pdl': None, 'pdc': None}
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=7d&interval=1d'
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=8)
        if resp.status_code != 200:
            return levels
        res = resp.json().get('chart', {}).get('result', [])
        if not res:
            return levels
        data = res[0]
        timestamps = data.get('timestamp', [])
        quote = data.get('indicators', {}).get('quote', [{}])[0]
        highs = quote.get('high', [])
        lows = quote.get('low', [])
        closes = quote.get('close', [])

        # Find previous trading day before for_date
        records = []
        for ts, h, l, c in zip(timestamps, highs, lows, closes):
            if ts is None or c is None:
                continue
            bar_date = datetime.datetime.fromtimestamp(ts, tz=_ET).date()
            if bar_date < for_date:
                records.append((bar_date, float(h or c), float(l or c), float(c)))

        if records:
            # Last record before for_date is previous day
            last_day = records[-1]
            levels['pdh'] = round(last_day[1], 2)
            levels['pdl'] = round(last_day[2], 2)
            levels['pdc'] = round(last_day[3], 2)
    except Exception as exc:
        log.warning("Could not fetch daily levels for %s: %s", ticker, exc)
    return levels


def fetch_stock_5m_bars(ticker: str, for_date: date, root: str = '') -> pd.DataFrame:
    """Fetch 5m bars for an individual stock from Yahoo Finance."""
    try:
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=5d&interval=5m'
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=8)
        if resp.status_code != 200:
            return pd.DataFrame()
        res = resp.json().get('chart', {}).get('result', [])
        if not res:
            return pd.DataFrame()
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
            return pd.DataFrame()

        df_all = pd.DataFrame(all_bars)
        # Select target date or fallback to most recent date available in data (e.g. weekends)
        available_dates = sorted(df_all['date'].unique())
        target = for_date if for_date in available_dates else (available_dates[-1] if available_dates else for_date)
        
        # Include up to 40 preceding bars for smooth EMA initialization
        target_idx = df_all[df_all['date'] == target].index
        if len(target_idx) == 0:
            return pd.DataFrame()
        first_target_pos = target_idx[0]
        start_pos = max(0, first_target_pos - 40)
        df_sub = df_all.iloc[start_pos:].copy()
        df_sub['is_today'] = (df_sub['date'] == target)
        return df_sub.drop(columns=['date']).reset_index(drop=True)
    except Exception as exc:
        log.warning("Could not fetch 5m bars for %s: %s", ticker, exc)
        return pd.DataFrame()


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
        onh=None,  # No overnight session needed for individual equities
        onl=None,
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
