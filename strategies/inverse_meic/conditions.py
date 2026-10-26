"""Market regime and gatekeeper condition evaluator for Inverse MEIC.

Monitors 4 market conditions:
1. Pre-Market Drop / Move: SPX pre-market gap <= -0.5% (or overnight ES futures move).
2. 30-Minute ORB Range: SPX 30-minute opening range (08:30–09:00 CT) >= 25.0 points.
3. Spot VIX: VIX level >= 18.0 or intraday surge >= 5.0%.
4. Realized ATR (14): 14-period daily ATR >= 25.0 points (elevated volatility).

Strategy Gates:
- At least 1 condition active -> STRATEGY ARMED (allows tranche entries).
- None active -> STRATEGY STANDBY (skips tranche entries today).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, Optional, Tuple

import requests

from meic0dte.app.utilities import central_date, central_time

log = logging.getLogger('inverse_meic_conditions')

DEFAULT_PREMARKET_GAP_THRESHOLD = -0.005   # -0.5%
DEFAULT_ORB_RANGE_THRESHOLD = 25.0         # 25 points
DEFAULT_VIX_THRESHOLD = 18.0              # VIX >= 18.0
DEFAULT_ATR_THRESHOLD = 25.0              # Realized ATR (14) >= 25.0


@dataclass
class MarketRegimeStatus:
    is_armed: bool
    regime: str  # 'ARMED' | 'STANDBY'
    premarket_gap_pct: float
    premarket_gap_target: float
    premarket_gap_met: bool
    orb_range_pts: float
    orb_range_threshold: float
    orb_range_met: bool
    spot_vix: float
    vix_threshold: float
    vix_met: bool
    realized_atr: float
    atr_threshold: float
    atr_met: bool
    summary_message: str
    today_mode: str = 'ACTIVE TRADING'
    next_tranche: str = '10-30'
    target_multiplier: float = 3.0
    evaluated_at: str = ''
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def _fetch_spx_and_vix_quotes(broker=None, prices=None) -> Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """Retrieve SPX spot, SPX prior close, VIX spot, and VIX prior close.

    Returns: (spx_spot, spx_close, vix_spot, vix_close)
    """
    spx_spot = None
    spx_close = None
    vix_spot = None
    vix_close = None

    # 1. From shared MQTT Price Cache if available
    if prices is not None:
        try:
            spx_spot = prices.get_spx() or prices.get('SPX')
            vix_spot = prices.get('VIX') or prices.get('$VIX')
        except Exception:
            pass

    # 2. From Broker REST API if available
    if broker is not None:
        try:
            if spx_spot is None and hasattr(broker, 'fetch_spx_price_api'):
                spx_spot = broker.fetch_spx_price_api()
            if spx_spot is None and hasattr(broker, 'get_spx_price'):
                spx_spot = broker.get_spx_price()
            if vix_spot is None and hasattr(broker, 'get_option_price'):
                vix_spot = broker.get_option_price('$VIX') or broker.get_option_price('VIX')
        except Exception:
            pass

    # 3. Yahoo Finance fallback for PDC and VIX
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        # SPX
        resp = requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=5d&interval=1d',
            headers=headers,
            timeout=5,
        )
        if resp.status_code == 200:
            res = resp.json().get('chart', {}).get('result', [])
            if res:
                meta = res[0].get('meta', {})
                spx_close = meta.get('chartPreviousClose') or meta.get('previousClose')
                if spx_spot is None:
                    spx_spot = meta.get('regularMarketPrice')

        # VIX
        resp_vix = requests.get(
            'https://query1.finance.yahoo.com/v8/finance/chart/%5EVIX?range=5d&interval=1d',
            headers=headers,
            timeout=5,
        )
        if resp_vix.status_code == 200:
            res_v = resp_vix.json().get('chart', {}).get('result', [])
            if res_v:
                meta_v = res_v[0].get('meta', {})
                vix_close = meta_v.get('chartPreviousClose') or meta_v.get('previousClose')
                if vix_spot is None:
                    vix_spot = meta_v.get('regularMarketPrice')
    except Exception as exc:
        log.debug('Yahoo Finance quote fallback exception: %s', exc)

    return spx_spot, spx_close, vix_spot, vix_close


def _fetch_orb_range(today: date, root: str = '') -> float:
    """Calculate SPX 30-min Opening Range Breakout (high - low between 08:30 and 09:00 CT)."""
    # 1. Try local Data/YYYY-MM-DD/SPX_5m.csv or SPX_1m.csv
    if root:
        today_dir = os.path.join(root, 'Data', today.isoformat())
        for fname in ('SPX_5m.csv', 'SPX_1m.csv'):
            path = os.path.join(today_dir, fname)
            if os.path.isfile(path):
                try:
                    import pandas as pd
                    df = pd.read_csv(path)
                    # Filter for 08:30 - 09:00 CT
                    if 'datetime' in df.columns:
                        df['dt'] = pd.to_datetime(df['datetime'])
                        orb_slice = df[(df['dt'].dt.time >= time(8, 30)) & (df['dt'].dt.time < time(9, 0))]
                        if not orb_slice.empty:
                            rng = float(orb_slice['high'].max() - orb_slice['low'].min())
                            return round(rng, 2)
                except Exception:
                    pass

    # 2. Try Yahoo Finance intraday 5m chart for ^GSPC
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=1d&interval=5m'
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            res = resp.json().get('chart', {}).get('result', [])
            if res:
                timestamps = res[0].get('timestamp', [])
                quote = res[0].get('indicators', {}).get('quote', [{}])[0]
                highs = quote.get('high', [])
                lows = quote.get('low', [])
                orb_highs = []
                orb_lows = []
                from zoneinfo import ZoneInfo
                ct_zone = ZoneInfo('America/Chicago')
                for ts, h, l in zip(timestamps, highs, lows):
                    if ts and h is not None and l is not None:
                        bar_time = datetime.fromtimestamp(ts, tz=ct_zone).time()
                        if time(8, 30) <= bar_time < time(9, 0):
                            orb_highs.append(h)
                            orb_lows.append(l)
                if orb_highs and orb_lows:
                    return round(float(max(orb_highs) - min(orb_lows)), 2)
    except Exception:
        pass

    return 0.0


def _fetch_realized_atr(today: date, period: int = 14) -> float:
    """Calculate 14-period Realized ATR on daily SPX bars."""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        url = f'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?range=1mo&interval=1d'
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            res = resp.json().get('chart', {}).get('result', [])
            if res:
                quote = res[0].get('indicators', {}).get('quote', [{}])[0]
                highs = quote.get('high', [])
                lows = quote.get('low', [])
                closes = quote.get('close', [])
                trs = []
                for i in range(1, len(closes)):
                    if highs[i] is not None and lows[i] is not None and closes[i - 1] is not None:
                        h = float(highs[i])
                        l = float(lows[i])
                        prev_c = float(closes[i - 1])
                        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                        trs.append(tr)
                if trs:
                    recent = trs[-period:] if len(trs) >= period else trs
                    return round(sum(recent) / len(recent), 2)
    except Exception:
        pass

    return 28.0  # Reasonable baseline fallback


def evaluate_market_conditions(
    broker=None,
    prices=None,
    root: str = '',
    gap_threshold: float = DEFAULT_PREMARKET_GAP_THRESHOLD,
    orb_threshold: float = DEFAULT_ORB_RANGE_THRESHOLD,
    vix_threshold: float = DEFAULT_VIX_THRESHOLD,
    atr_threshold: float = DEFAULT_ATR_THRESHOLD,
    target_multiplier: float = 3.0,
    force_armed: Optional[bool] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> MarketRegimeStatus:
    """Evaluate market regime conditions and determine if Inverse MEIC is ARMED or STANDBY."""
    overrides = overrides or {}
    today = central_date()
    now_str = central_time().strftime('%Y-%m-%d %H:%M:%S CT')

    spx_spot, spx_close, vix_spot, vix_close = _fetch_spx_and_vix_quotes(broker=broker, prices=prices)

    # 1. Pre-market Gap %
    spx_spot_val = _safe_float(overrides.get('spx_spot', spx_spot), 5800.0)
    spx_close_val = _safe_float(overrides.get('spx_close', spx_close), 5820.0)
    if spx_close_val > 0:
        gap_pct = round((spx_spot_val - spx_close_val) / spx_close_val, 4)
    else:
        gap_pct = 0.0

    gap_thresh = _safe_float(overrides.get('gap_threshold', gap_threshold), DEFAULT_PREMARKET_GAP_THRESHOLD)
    premarket_gap_met = (gap_pct <= gap_thresh)

    # 2. 30-min ORB Range
    orb_range = _safe_float(overrides.get('orb_range'), None)
    if orb_range is None:
        orb_range = _fetch_orb_range(today, root=root)
    orb_thresh = _safe_float(overrides.get('orb_threshold', orb_threshold), DEFAULT_ORB_RANGE_THRESHOLD)
    orb_range_met = (orb_range >= orb_thresh)

    # 3. Spot VIX
    spot_vix_val = _safe_float(overrides.get('vix_spot', vix_spot), 17.50)
    vix_close_val = _safe_float(overrides.get('vix_close', vix_close), 17.00)
    vix_thresh = _safe_float(overrides.get('vix_threshold', vix_threshold), DEFAULT_VIX_THRESHOLD)
    vix_spike = False
    if vix_close_val > 0:
        vix_spike = ((spot_vix_val - vix_close_val) / vix_close_val) >= 0.05
    vix_met = (spot_vix_val >= vix_thresh) or vix_spike

    # 4. Realized ATR (14)
    realized_atr_val = _safe_float(overrides.get('atr', None), None)
    if realized_atr_val is None:
        realized_atr_val = _fetch_realized_atr(today)
    atr_thresh = _safe_float(overrides.get('atr_threshold', atr_threshold), DEFAULT_ATR_THRESHOLD)
    atr_met = (realized_atr_val >= atr_thresh)

    # Gatekeeper Evaluation
    active_conditions = []
    if premarket_gap_met:
        active_conditions.append(f'Pre-market Gap ({gap_pct*100:+.2f}%)')
    if orb_range_met:
        active_conditions.append(f'30m ORB ({orb_range:.1f} pts)')
    if vix_met:
        active_conditions.append(f'VIX ({spot_vix_val:.2f})')
    if atr_met:
        active_conditions.append(f'ATR14 ({realized_atr_val:.1f} pts)')

    if force_armed is not None:
        is_armed = bool(force_armed)
        reason = 'Manually Forced'
    else:
        is_armed = len(active_conditions) > 0
        reason = ', '.join(active_conditions) if is_armed else 'Low Volatility / Normal Consolidation'

    regime = 'ARMED' if is_armed else 'STANDBY'
    today_mode = 'ACTIVE TRADING' if is_armed else 'STANDBY (MONITORING)'

    summary_message = (
        f"Regime: {regime} — {len(active_conditions)} condition(s) met: {reason}"
        if is_armed
        else f"Regime: STANDBY — Volatility below entry expansion thresholds."
    )

    return MarketRegimeStatus(
        is_armed=is_armed,
        regime=regime,
        premarket_gap_pct=gap_pct,
        premarket_gap_target=gap_thresh,
        premarket_gap_met=premarket_gap_met,
        orb_range_pts=orb_range,
        orb_range_threshold=orb_thresh,
        orb_range_met=orb_range_met,
        spot_vix=spot_vix_val,
        vix_threshold=vix_thresh,
        vix_met=vix_met,
        realized_atr=realized_atr_val,
        atr_threshold=atr_thresh,
        atr_met=atr_met,
        summary_message=summary_message,
        today_mode=today_mode,
        next_tranche='10-30',
        target_multiplier=target_multiplier,
        evaluated_at=now_str,
        details={
            'spx_spot': spx_spot_val,
            'spx_close': spx_close_val,
            'vix_close': vix_close_val,
            'active_conditions_count': len(active_conditions),
            'active_conditions': active_conditions,
        },
    )


def save_regime_snapshot(root: str, status: MarketRegimeStatus) -> str:
    from common import trades_layout
    dest_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_INVERSE_MEIC, root)
    os.makedirs(dest_dir, exist_ok=True)
    today = central_date().isoformat()
    path = os.path.join(dest_dir, f'regime_{today}.json')
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(status.to_dict(), f, indent=2)
    os.replace(tmp, path)
    return path


def load_regime_snapshot(root: str) -> Optional[Dict[str, Any]]:
    from common import trades_layout
    today = central_date().isoformat()
    dest_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_INVERSE_MEIC, root)
    path = os.path.join(dest_dir, f'regime_{today}.json')
    if os.path.isfile(path):
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None
