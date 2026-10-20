"""Market data feeding, Yahoo Finance historical level fetcher, and CSV bar loader."""
from __future__ import annotations

import csv
import datetime
import logging
import os
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from strategies.drma_hm.config import DRMAHMConfig

log = logging.getLogger(__name__)

_ET = ZoneInfo('America/New_York')
_CT = ZoneInfo('America/Chicago')
_DEFAULT_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
}


def _find_prev_trading_day_dir(data_root: str, current_date: date) -> Optional[Tuple[date, str]]:
    """Scan local data directory to find the most recent trading date prior to current_date."""
    if not os.path.isdir(data_root):
        return None
    for offset in range(1, 10):
        prev_d = current_date - timedelta(days=offset)
        candidate = os.path.join(data_root, prev_d.isoformat(), 'SPX_5m.csv')
        if os.path.isfile(candidate):
            return prev_d, candidate
    return None


def fetch_yahoo_market_levels(
    today: date,
    cfg: DRMAHMConfig,
    root: str,
) -> Dict[str, Optional[float]]:
    """Fetch overnight ES futures levels and previous day SPX levels.

    Computes:
      - ES Overnight High (ONH), Overnight Low (ONL)
      - SPX Previous Day High (PDH), Low (PDL), Close (PDC)
      - ES-to-SPX Basis Offset (scaled ES close - SPX close at previous RTH close)
      - Scaled ONH & ONL aligned with SPX cash index price

    Provides automatic fallbacks:
      1. Uses manual overrides if specified in cfg (manual_onh, manual_onl, manual_pdh, etc.)
      2. If Yahoo Finance is disabled or fails, reads previous day's local SPX_5m.csv.
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

    # 1. Try Yahoo Finance if enabled
    if cfg.enable_yahoo_finance:
        try:
            # Query ES futures 5m bars for overnight session
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

                    # Query SPX daily chart for PDC, PDH, PDL
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

                            # Match previous day
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
                    log.info('Successfully fetched levels from Yahoo Finance: %s', levels)
        except Exception as exc:
            log.warning('Yahoo Finance fetch exception: %s — falling back to local files', exc)

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

    return levels


def fetch_yahoo_spx_5m_bars(for_date: date) -> pd.DataFrame:
    """Fetch live 5m bars for SPX (^GSPC) from Yahoo Finance as a resilient fallback."""
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=1d&interval=5m'
        resp = requests.get(url, headers=_DEFAULT_HEADERS, timeout=8)
        if resp.status_code != 200:
            return pd.DataFrame()
        res = resp.json().get('chart', {}).get('result', [])
        if not res:
            return pd.DataFrame()
        ts = res[0].get('timestamp', [])
        quote = res[0].get('indicators', {}).get('quote', [{}])[0]
        opens = quote.get('open', [])
        highs = quote.get('high', [])
        lows = quote.get('low', [])
        closes = quote.get('close', [])
        bars = []
        for t, o, h, l, c in zip(ts, opens, highs, lows, closes):
            if c is None or o is None or h is None or l is None:
                continue
            dt_ct = datetime.datetime.fromtimestamp(t, tz=_CT)
            if dt_ct.date() != for_date:
                continue
            bars.append({
                'datetime': dt_ct.strftime('%Y-%m-%d %H:%M:%S'),
                'open': round(float(o), 2),
                'high': round(float(h), 2),
                'low': round(float(l), 2),
                'close': round(float(c), 2),
                'samples': '',
                'sma_9': '',
                'sma_20': '',
                'sma_50': '',
                'sma_200': '',
                'ema_9': '',
                'ema_12': '',
                'ema_21': '',
                'ema_26': '',
                'ema_50': '',
            })
        if not bars:
            return pd.DataFrame()
        return pd.DataFrame(bars)
    except Exception as exc:
        log.warning('Failed to fetch Yahoo SPX 5m bars: %s', exc)
        return pd.DataFrame()


def load_spx_5m_bars(
    data_root: str,
    for_date: date,
    prepend_prev_bars: int = 40,
) -> pd.DataFrame:
    """Load deduplicated SPX 5m bars for for_date, optionally prepending tail of prior day.

    Returns a DataFrame with columns: ['datetime', 'open', 'high', 'low', 'close']
    with an added boolean column 'is_today'.
    """
    day_csv = os.path.join(data_root, for_date.isoformat(), 'SPX_5m.csv')
    df_today = pd.DataFrame()

    if os.path.isfile(day_csv):
        try:
            df_today = pd.read_csv(day_csv, on_bad_lines='skip')
        except Exception as exc:
            log.warning('Could not read %s: %s', day_csv, exc)

    # Fall back to live Yahoo Finance if local CSV is missing, empty, or has fewer than 2 bars
    if df_today.empty or len(df_today) < 2 or 'datetime' not in df_today.columns:
        df_yahoo = fetch_yahoo_spx_5m_bars(for_date)
        if not df_yahoo.empty:
            df_today = df_yahoo
            try:
                os.makedirs(os.path.dirname(day_csv), exist_ok=True)
                df_today.to_csv(day_csv, index=False)
                log.info('Populated %d SPX 5m bars from Yahoo Finance to %s', len(df_today), day_csv)
            except Exception as exc:
                log.warning('Could not write backfilled SPX bars: %s', exc)

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
) -> Optional[float]:
    """Fetch live/current ES futures price via TastyTrade streamer / MQTT cache.

    Order of precedence:
      1. Broker API quote (TastyTrade Streamer / MQTT cache or REST)
      2. Direct MQTT price cache lookup ('ES', '/ES')
      3. SPX cash spot + ES basis offset fallback (ONLY if allow_synthetic_fallback=True)
    """
    # 1. Broker API (TastyTrade Streamer / MQTT / REST)
    if broker is not None:
        try:
            if hasattr(broker, 'get_future_price'):
                p = broker.get_future_price('ES')
                if p is not None and p > 0:
                    return round(float(p), 2)
        except Exception as b_exc:
            log.debug('Broker ES quote attempt: %s', b_exc)

    # 2. Direct MQTT price cache lookup
    try:
        from common.mqtt_prices import get_mqtt_price_cache
        cache = get_mqtt_price_cache()
        if cache:
            for s in ('ES', '/ES', '/ES:XCME'):
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

