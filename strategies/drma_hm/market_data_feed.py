"""Market data feeding, morning ONH/ONL level fetcher, and TastyTrade CSV/DXLink bar loader."""
from __future__ import annotations

import asyncio
import csv
import datetime
import logging
import os
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from strategies.drma_hm.config import DRMAHMConfig

log = logging.getLogger(__name__)

_ET = ZoneInfo('America/New_York')
_CT = ZoneInfo('America/Chicago')
_UTC = ZoneInfo('UTC')
_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}

_LAST_TT_CANDLE_BACKFILL_ATTEMPT: Dict[str, float] = {}
_TT_CANDLE_BACKFILL_COOLDOWN_SEC = 60.0


def _find_prev_trading_day_dir(
    data_root: str,
    current_date: date,
    filename: str = 'SPX_5m.csv',
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


def _compute_local_overnight_levels(
    data_root: str,
    today: date,
    symbol: str = 'ES',
    es_offset: float = 0.0,
) -> Tuple[Optional[float], Optional[float]]:
    """Compute ONH and ONL from locally recorded TastyTrade ES/MES 5m CSV bars."""
    sym = (symbol or 'ES').upper().lstrip('/')
    fname = f'{sym}_5m.csv'
    highs: List[float] = []
    lows: List[float] = []

    # 1. Previous trading day bars from 15:00 CST onwards (post-cash close / Globex open)
    prev_info = _find_prev_trading_day_dir(data_root, today, filename=fname)
    if prev_info:
        _, prev_csv = prev_info
        try:
            df_prev = pd.read_csv(prev_csv, on_bad_lines='skip')
            if not df_prev.empty and 'datetime' in df_prev.columns:
                for _, row in df_prev.iterrows():
                    dt_str = str(row.get('datetime', ''))
                    if len(dt_str) >= 16 and dt_str[11:16] >= '15:00':
                        highs.append(float(row['high']))
                        lows.append(float(row['low']))
        except Exception as exc:
            log.debug('Could not read prev day %s for local ONH/ONL: %s', prev_csv, exc)

    # 2. Today's bars prior to 08:30 CST cash open
    day_csv = os.path.join(data_root, today.isoformat(), fname)
    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
            if not df_today.empty and 'datetime' in df_today.columns:
                for _, row in df_today.iterrows():
                    dt_str = str(row.get('datetime', ''))
                    if len(dt_str) >= 16 and dt_str[11:16] < '08:30':
                        highs.append(float(row['high']))
                        lows.append(float(row['low']))
        except Exception as exc:
            log.debug('Could not read today %s for local ONH/ONL: %s', day_csv, exc)

    if not highs or not lows:
        return None, None
    return round(max(highs) - es_offset, 2), round(min(lows) - es_offset, 2)


def fetch_yahoo_market_levels(
    today: date,
    cfg: DRMAHMConfig,
    root: str,
    *,
    allow_yahoo: bool = True,
) -> Dict[str, Optional[float]]:
    """Fetch overnight ES futures levels (ONH, ONL) and previous day SPX levels (PDH, PDL, PDC).

    Yahoo Finance is ONLY queried when `allow_yahoo=True` (used once around 08:30 AM CST
    to grab ONL and ONH for the morning session). Outside that window or if disabled/offline,
    levels are populated from local TastyTrade-recorded CSV files (`SPX_5m.csv` and `ES_5m.csv`).
    """
    levels: Dict[str, Optional[float]] = {
        'pdh': cfg.manual_pdh,
        'pdl': cfg.manual_pdl,
        'pdc': cfg.manual_pdc,
        'onh': cfg.manual_onh,
        'onl': cfg.manual_onl,
        'es_offset': cfg.manual_es_offset,
        'source': 'manual' if any([cfg.manual_pdh, cfg.manual_onh]) else 'uninitialized',
    }

    # If all levels are manually set, return immediately
    if all(levels[k] is not None for k in ['pdh', 'pdl', 'pdc', 'onh', 'onl']):
        log.info('Using fully manual market levels: %s', levels)
        return levels

    data_root = os.path.join(root, cfg.data_dir)

    # 1. Try Yahoo Finance ONLY when explicitly allowed (morning 08:30 AM CST grab) and enabled
    if cfg.enable_yahoo_finance and allow_yahoo:
        try:
            # Query ES futures 5m bars for overnight session ONH / ONL
            es_url = 'https://query1.finance.yahoo.com/v8/finance/chart/ES=F?range=5d&interval=5m'
            resp_es = requests.get(es_url, headers=_DEFAULT_HEADERS, timeout=8)
            if resp_es.status_code == 200:
                res = resp_es.json().get('chart', {}).get('result', [])
                if res:
                    es_data = res[0]
                    timestamps = es_data.get('timestamp', [])
                    quote = es_data.get('indicators', {}).get('quote', [{}])[0]
                    highs = quote.get('high', [])
                    lows = quote.get('low', [])
                    closes = quote.get('close', [])

                    # Determine target session date in ET (if queried on weekend, target next trading day)
                    target_date = today
                    if target_date.weekday() == 6:  # Sunday -> targets Monday
                        target_date = target_date + timedelta(days=1)
                    elif target_date.weekday() == 5:  # Saturday -> targets Monday
                        target_date = target_date + timedelta(days=2)

                    today_et_start = datetime.datetime.combine(target_date, datetime.time(9, 30), tzinfo=_ET)

                    # Determine previous RTH trading day
                    prev_rth_end = target_date - timedelta(days=1)
                    while prev_rth_end.weekday() >= 5:
                        prev_rth_end -= timedelta(days=1)
                    prev_rth_close_dt = datetime.datetime.combine(prev_rth_end, datetime.time(16, 0), tzinfo=_ET)

                    # For Monday session, CME Globex overnight session opens Sunday at 18:00 ET (17:00 CT).
                    # For Tue-Fri, overnight session begins at previous day's RTH close (16:00 ET).
                    if target_date.weekday() == 0:
                        sunday_date = target_date - timedelta(days=1)
                        overnight_start_dt = datetime.datetime.combine(sunday_date, datetime.time(18, 0), tzinfo=_ET)
                    else:
                        overnight_start_dt = prev_rth_close_dt

                    es_overnight_highs = []
                    es_overnight_lows = []
                    es_prev_close = None

                    for ts, h, l, c in zip(timestamps, highs, lows, closes):
                        if ts is None or h is None or l is None or c is None:
                            continue
                        dt_bar = datetime.datetime.fromtimestamp(ts, tz=_ET)

                        # Check near previous RTH close (15:55 to 16:05 ET)
                        if abs((dt_bar - prev_rth_close_dt).total_seconds()) <= 600:
                            es_prev_close = c

                        # Overnight window: from overnight start to target session 09:30 ET open
                        if overnight_start_dt <= dt_bar < today_et_start:
                            es_overnight_highs.append(h)
                            es_overnight_lows.append(l)

                    # Query SPX daily chart for PDC, PDH, PDL if not already available
                    spx_url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d'
                    resp_spx = requests.get(spx_url, headers=_DEFAULT_HEADERS, timeout=8)
                    if resp_spx.status_code == 200:
                        spx_res = resp_spx.json().get('chart', {}).get('result', [])
                        if spx_res:
                            spx_data = spx_res[0]
                            spx_ts = spx_data.get('timestamp', [])
                            spx_q = spx_data.get('indicators', {}).get('quote', [{}])[0]
                            spx_h = spx_q.get('high', [])
                            spx_l = spx_q.get('low', [])
                            spx_c = spx_q.get('close', [])

                            prev_date = prev_rth_end
                            for ts, h, l, c in zip(spx_ts, spx_h, spx_l, spx_c):
                                if ts is None or c is None:
                                    continue
                                dt_d = datetime.datetime.fromtimestamp(ts, tz=_ET).date()
                                if dt_d == prev_date:
                                    if levels['pdc'] is None:
                                        levels['pdc'] = round(float(c), 2)
                                    if levels['pdh'] is None:
                                        levels['pdh'] = round(float(h), 2)
                                    if levels['pdl'] is None:
                                        levels['pdl'] = round(float(l), 2)

                    # Calculate ES Basis offset and scaled ONH/ONL
                    if es_prev_close is not None and levels['pdc'] is not None and levels['es_offset'] is None:
                        levels['es_offset'] = round(float(es_prev_close) - float(levels['pdc']), 2)

                    offset = levels['es_offset'] or 0.0
                    if es_overnight_highs and levels['onh'] is None:
                        levels['onh'] = round(max(es_overnight_highs) - offset, 2)
                    if es_overnight_lows and levels['onl'] is None:
                        levels['onl'] = round(min(es_overnight_lows) - offset, 2)

                    levels['source'] = 'yahoo_finance'
                    log.info('Successfully fetched morning ONH/ONL levels from Yahoo Finance: %s', levels)
        except Exception as exc:
            log.warning('Yahoo Finance morning levels fetch exception: %s — falling back to local files', exc)

    # 2. Local CSV fallback for any missing PDH / PDL / PDC
    prev_info = _find_prev_trading_day_dir(data_root, today)
    if prev_info and (levels['pdh'] is None or levels['pdl'] is None or levels['pdc'] is None):
        prev_date, prev_csv_path = prev_info
        try:
            df_prev = pd.read_csv(prev_csv_path)
            if not df_prev.empty and 'close' in df_prev.columns:
                dedup = df_prev.drop_duplicates(subset=['datetime'], keep='last')
                if levels['pdh'] is None:
                    levels['pdh'] = round(float(dedup['high'].max()), 2)
                if levels['pdl'] is None:
                    levels['pdl'] = round(float(dedup['low'].min()), 2)
                if levels['pdc'] is None:
                    levels['pdc'] = round(float(dedup.iloc[-1]['close']), 2)
                if levels['source'] == 'uninitialized':
                    levels['source'] = f'local_csv_{prev_date.isoformat()}'
                log.info('Filled previous day levels from local file %s: %s', prev_csv_path, levels)
        except Exception as exc:
            log.warning('Failed to load local previous day CSV: %s', exc)

    # 3. Local TastyTrade ES/MES CSV fallback for ONH / ONL if not yet populated
    if levels['onh'] is None or levels['onl'] is None:
        fut_sym = getattr(cfg, 'futures_symbol', 'ES') or 'ES'
        loc_onh, loc_onl = _compute_local_overnight_levels(
            data_root,
            today,
            symbol=fut_sym,
            es_offset=float(levels.get('es_offset') or 0.0),
        )
        if levels['onh'] is None and loc_onh is not None:
            levels['onh'] = loc_onh
        if levels['onl'] is None and loc_onl is not None:
            levels['onl'] = loc_onl
        if (loc_onh is not None or loc_onl is not None) and levels['source'] == 'uninitialized':
            levels['source'] = f'local_tastytrade_{fut_sym.lower()}_csv'

    return levels


def fetch_yahoo_spx_5m_bars(for_date: date) -> pd.DataFrame:
    """Deprecated: Yahoo Finance is not used for real-time 5m bars."""
    return pd.DataFrame()


async def _fetch_tt_futures_candles_async(
    product_code: str,
    start_dt_utc: datetime.datetime,
    timeout_sec: float = 6.0,
) -> List[Dict[str, Any]]:
    from common.tt_auth import create_tastytrade_session
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Candle
    from tastytrade.instruments import Future

    session = create_tastytrade_session(paper=False)
    futs = await Future.get(session, product_codes=[product_code])
    if not isinstance(futs, list):
        futs = [futs]
    active = [f for f in futs if getattr(f, 'active', False)]
    front = next((f for f in active if getattr(f, 'active_month', False)), None)
    if not front and active:
        front = active[0]
    if not front:
        return []
    streamer_sym = (getattr(front, 'streamer_symbol', '') or '').strip()
    if not streamer_sym:
        return []

    by_ts: Dict[int, Dict[str, Any]] = {}
    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe_candle([streamer_sym], '5m', start_dt_utc)
        deadline = time.monotonic() + timeout_sec
        quiet_after_first = 1.2
        last_rx = None
        while time.monotonic() < deadline:
            rem = max(0.1, deadline - time.monotonic())
            if last_rx is not None:
                rem = min(rem, max(0.1, quiet_after_first - (time.monotonic() - last_rx)))
            try:
                candle = await asyncio.wait_for(streamer.get_event(Candle), timeout=rem)
            except asyncio.TimeoutError:
                if by_ts:
                    break
                continue
            if candle is None:
                continue
            last_rx = time.monotonic()
            c_open = float(candle.open or 0)
            c_high = float(candle.high or 0)
            c_low = float(candle.low or 0)
            c_close = float(candle.close or 0)
            if c_close <= 0 or c_open <= 0:
                continue
            ts_ms = int(candle.time or 0)
            if ts_ms <= 0:
                continue
            ts_sec = ts_ms // 1000 if ts_ms > 10_000_000_000 else ts_ms
            if ts_sec < int(start_dt_utc.timestamp()):
                continue
            by_ts[ts_sec] = {
                'ts_sec': ts_sec,
                'open': round(c_open, 2),
                'high': round(c_high, 2),
                'low': round(c_low, 2),
                'close': round(c_close, 2),
                'volume': int(float(candle.volume or 0)),
            }

    return [by_ts[k] for k in sorted(by_ts.keys())]


def fetch_tastytrade_es_5m_bars(
    for_date: date,
    symbol: str = 'ES',
    include_overnight: bool = True,
) -> pd.DataFrame:
    """Fetch 5m futures bars (ES or MES) directly from TastyTrade DXLink Candle stream."""
    if os.environ.get('PYTEST_CURRENT_TEST'):
        return pd.DataFrame()

    code = (symbol or 'ES').upper().lstrip('/')
    if code not in ('ES', 'MES'):
        code = 'ES'

    now_mono = time.monotonic()
    last_attempt = _LAST_TT_CANDLE_BACKFILL_ATTEMPT.get(code, 0.0)
    if (now_mono - last_attempt) < _TT_CANDLE_BACKFILL_COOLDOWN_SEC:
        return pd.DataFrame()
    _LAST_TT_CANDLE_BACKFILL_ATTEMPT[code] = now_mono

    prev_d = for_date - timedelta(days=1)
    while prev_d.weekday() >= 5:
        prev_d -= timedelta(days=1)
    cycle_start_ct = (
        datetime.datetime.combine(prev_d, datetime.time(17, 0), tzinfo=_CT)
        if include_overnight
        else datetime.datetime.combine(for_date, datetime.time(0, 0), tzinfo=_CT)
    )
    start_utc = cycle_start_ct.astimezone(_UTC)

    try:
        raw_candles = asyncio.run(_fetch_tt_futures_candles_async(code, start_utc))
    except Exception as exc:
        log.warning('Failed to backfill TastyTrade %s 5m candles via DXLink: %s', code, exc)
        return pd.DataFrame()

    if not raw_candles:
        return pd.DataFrame()

    bars = []
    for item in raw_candles:
        dt_ct = datetime.datetime.fromtimestamp(item['ts_sec'], tz=_CT)
        if dt_ct < cycle_start_ct or dt_ct.date() > for_date:
            continue
        bars.append({
            'datetime': dt_ct.strftime('%Y-%m-%d %H:%M:%S'),
            'open': item['open'],
            'high': item['high'],
            'low': item['low'],
            'close': item['close'],
            'volume': item.get('volume', 0),
            'is_today': True,
        })

    if not bars:
        return pd.DataFrame()
    log.info('Backfilled %d %s 5m bars from TastyTrade DXLink', len(bars), code)
    return pd.DataFrame(bars)


def load_es_5m_bars(
    data_root: str,
    for_date: date,
    symbol: str = 'ES',
    prepend_prev_bars: int = 40,
    include_overnight: bool = True,
    allow_backfill: bool = True,
) -> pd.DataFrame:
    """Load deduplicated ES or MES 5m bars recorded from TastyTrade streamer.

    Combines the current overnight Globex session (from 17:00 CST on the prior trading day
    through `for_date`, marked `is_today=True`) with optional earlier bars (`is_today=False`)
    for indicator warmup. Falls back to TastyTrade DXLink Candle backfill on cold start if
    local CSV files have fewer than 2 bars. Never queries Yahoo Finance.
    """
    sym = (symbol or 'ES').upper().lstrip('/')
    if sym not in ('ES', 'MES'):
        sym = 'ES'

    # Try configured symbol first, then alternate ES/MES if primary CSV does not exist yet
    alt_sym = 'MES' if sym == 'ES' else 'ES'
    fname = f'{sym}_5m.csv'
    day_csv = os.path.join(data_root, for_date.isoformat(), fname)
    if not os.path.isfile(day_csv):
        alt_csv = os.path.join(data_root, for_date.isoformat(), f'{alt_sym}_5m.csv')
        if os.path.isfile(alt_csv):
            fname = f'{alt_sym}_5m.csv'
            day_csv = alt_csv

    df_today = pd.DataFrame()
    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
        except Exception as exc:
            log.warning('Could not read %s: %s', day_csv, exc)

    prev_info = _find_prev_trading_day_dir(data_root, for_date, filename=fname)
    df_prev = pd.DataFrame()
    if prev_info:
        _, prev_csv_path = prev_info
        try:
            df_prev = pd.read_csv(prev_csv_path, on_bad_lines='skip')
        except Exception as exc:
            log.debug('Could not read previous day futures CSV %s: %s', prev_csv_path, exc)

    frames: List[pd.DataFrame] = []
    if not df_prev.empty and 'datetime' in df_prev.columns:
        df_prev_dedup = df_prev.drop_duplicates(subset=['datetime'], keep='last').copy()
        if include_overnight:
            time_part = df_prev_dedup['datetime'].astype(str).str.slice(11, 16)
            prev_overnight = df_prev_dedup[time_part >= '17:00'].copy()
            prev_daytime = df_prev_dedup[time_part < '17:00'].tail(max(0, prepend_prev_bars)).copy()
            if not prev_daytime.empty:
                prev_daytime['is_today'] = False
                frames.append(prev_daytime[['datetime', 'open', 'high', 'low', 'close', 'is_today']])
            if not prev_overnight.empty:
                prev_overnight['is_today'] = True
                frames.append(prev_overnight[['datetime', 'open', 'high', 'low', 'close', 'is_today']])
        elif prepend_prev_bars > 0:
            prev_tail = df_prev_dedup.tail(prepend_prev_bars).copy()
            prev_tail['is_today'] = False
            frames.append(prev_tail[['datetime', 'open', 'high', 'low', 'close', 'is_today']])

    if not df_today.empty and 'datetime' in df_today.columns:
        df_today_dedup = df_today.drop_duplicates(subset=['datetime'], keep='last').copy()
        # If we are in the evening session (>= 17:00 CST on for_date), the new Globex cycle starts at 17:00 CST
        time_part = df_today_dedup['datetime'].astype(str).str.slice(11, 16)
        has_evening = (time_part >= '17:00').any()
        if has_evening and include_overnight:
            earlier = df_today_dedup[time_part < '17:00'].tail(max(0, prepend_prev_bars)).copy()
            evening = df_today_dedup[time_part >= '17:00'].copy()
            frames.clear()
            if not earlier.empty:
                earlier['is_today'] = False
                frames.append(earlier[['datetime', 'open', 'high', 'low', 'close', 'is_today']])
            if not evening.empty:
                evening['is_today'] = True
                frames.append(evening[['datetime', 'open', 'high', 'low', 'close', 'is_today']])
        else:
            df_today_dedup['is_today'] = True
            frames.append(df_today_dedup[['datetime', 'open', 'high', 'low', 'close', 'is_today']])

    combined = (
        pd.concat(frames, ignore_index=True).drop_duplicates(subset=['datetime'], keep='last').reset_index(drop=True)
        if frames
        else pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'is_today'])
    )

    # Cold-start backfill from TastyTrade DXLink Candles if local CSV has < 2 bars
    if len(combined) < 2 and allow_backfill:
        df_tt = fetch_tastytrade_es_5m_bars(for_date, symbol=sym, include_overnight=include_overnight)
        if not df_tt.empty:
            return df_tt[['datetime', 'open', 'high', 'low', 'close', 'is_today']].reset_index(drop=True)

    return combined


def fetch_yahoo_es_5m_bars(
    for_date: date,
    include_overnight: bool = True,
    data_root: Optional[str] = None,
    symbol: str = 'ES',
) -> pd.DataFrame:
    """Backward-compatible wrapper that loads ES/MES 5m bars from TastyTrade (never Yahoo Finance)."""
    root_dir = data_root or os.path.join(
        os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')),
        'data',
    )
    return load_es_5m_bars(
        root_dir,
        for_date,
        symbol=symbol,
        include_overnight=include_overnight,
    )


def load_spx_5m_bars(
    data_root: str,
    for_date: date,
    prepend_prev_bars: int = 40,
) -> pd.DataFrame:
    """Load deduplicated SPX 5m bars for for_date recorded from TastyTrade, prepending tail of prior day.

    Returns a DataFrame with columns: ['datetime', 'open', 'high', 'low', 'close']
    with an added boolean column 'is_today'. Never queries Yahoo Finance for real-time bars.
    """
    day_csv = os.path.join(data_root, for_date.isoformat(), 'SPX_5m.csv')
    df_today = pd.DataFrame()

    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
        except Exception as exc:
            log.warning('Could not read %s: %s', day_csv, exc)

    if df_today.empty or 'datetime' not in df_today.columns:
        return pd.DataFrame(columns=['datetime', 'open', 'high', 'low', 'close', 'is_today'])

    df_today = df_today.drop_duplicates(subset=['datetime'], keep='last').copy()
    df_today['is_today'] = True

    if prepend_prev_bars <= 0:
        return df_today[['datetime', 'open', 'high', 'low', 'close', 'is_today']].reset_index(drop=True)

    prev_info = _find_prev_trading_day_dir(data_root, for_date)
    if not prev_info:
        return df_today[['datetime', 'open', 'high', 'low', 'close', 'is_today']].reset_index(drop=True)

    _, prev_csv_path = prev_info
    try:
        df_prev = pd.read_csv(prev_csv_path)
        if not df_prev.empty and 'datetime' in df_prev.columns:
            df_prev_dedup = df_prev.drop_duplicates(subset=['datetime'], keep='last').copy()
            df_prev_tail = df_prev_dedup.tail(prepend_prev_bars).copy()
            df_prev_tail['is_today'] = False
            combined = pd.concat([df_prev_tail, df_today], ignore_index=True)
            return combined[['datetime', 'open', 'high', 'low', 'close', 'is_today']].reset_index(drop=True)
    except Exception as exc:
        log.debug('Error prepending previous day bars: %s', exc)

    return df_today[['datetime', 'open', 'high', 'low', 'close', 'is_today']].reset_index(drop=True)


def fetch_es_current_price(
    broker: Optional[Any] = None,
    levels: Optional[Dict[str, Optional[float]]] = None,
    spx_spot: Optional[float] = None,
    timeout_sec: float = 4.0,
    es_basis: Optional[float] = None,
    allow_synthetic_fallback: bool = True,
    symbol: str = 'ES',
) -> Optional[float]:
    """Fetch live/current ES or MES futures price via TastyTrade streamer / MQTT cache.

    Order of precedence:
      1. Broker API quote (TastyTrade Streamer / MQTT cache or REST)
      2. Direct MQTT price cache lookup ('ES', '/ES', 'MES', '/MES')
      3. SPX cash spot + ES basis offset fallback (ONLY if allow_synthetic_fallback=True)
    Never queries Yahoo Finance for live prices.
    """
    sym = (symbol or 'ES').upper().lstrip('/')
    if sym not in ('ES', 'MES'):
        sym = 'ES'

    # 1. Broker API (TastyTrade Streamer / MQTT / REST)
    if broker is not None:
        try:
            if hasattr(broker, 'get_future_price'):
                p = broker.get_future_price(sym)
                if p is not None and p > 0:
                    return round(float(p), 2)
        except Exception as b_exc:
            log.debug('Broker %s quote attempt: %s', sym, b_exc)

    # 2. Direct MQTT price cache lookup
    try:
        from common.mqtt_prices import get_mqtt_price_cache
        cache = get_mqtt_price_cache()
        if cache:
            candidates = [sym, f'/{sym}', f'/{sym}:XCME']
            for alt in ('ES', '/ES', '/ES:XCME', 'MES', '/MES', '/MES:XCME'):
                if alt not in candidates:
                    candidates.append(alt)
            for s in candidates:
                p = cache.get(s)
                if p is not None and p > 0:
                    return round(float(p), 2)
    except Exception as m_exc:
        log.debug('Direct MQTT price cache lookup: %s', m_exc)

    # 3. SPX spot + basis offset fallback (only when explicitly allowed, e.g. offline testing)
    if allow_synthetic_fallback and spx_spot is not None and spx_spot > 0:
        offset = es_basis if es_basis is not None else ((levels.get('es_offset') if levels else None) or (levels.get('es_basis') if levels else None) or 0.0)
        return round(float(spx_spot) + float(offset), 2)

    return None
