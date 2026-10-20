"""Indicator engine: Daily Reset MA, 30m ORB, Hilega-Milega MTF, and Confluence Scoring."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


@dataclass
class IndicatorSnapshot:
    bar_index: int
    datetime_str: str
    close: float
    open: float
    high: float
    low: float
    out_ma: float
    ma_delta_2: float
    ma_slope_up: bool
    ma_slope_down: bool
    is_not_extended: bool
    orb_high: Optional[float]
    orb_low: Optional[float]
    orb_mid: Optional[float]
    orb_formed: bool
    pdh: Optional[float]
    pdl: Optional[float]
    pdc: Optional[float]
    onh: Optional[float]
    onl: Optional[float]
    rsi: float
    hm_fast: float
    hm_slow: float
    is_super_bull: bool
    is_super_bear: bool
    is_bull_pullback: bool
    is_bear_bounce: bool
    bull_score: int
    bear_score: int
    active_score: int
    bias: str               # 'BULL', 'BEAR', 'NEUTRAL'
    setup_grade: str        # 'GRADE A+ (5/5)', 'GRADE A (4/5)', 'GRADE B (3/5)', 'CHOP'
    trade_vehicle: str      # 'BUY CALLS / CDS', 'SELL PCS', 'BUY PUTS / PDS', 'SELL CCS', 'IRON CONDOR / CASH'
    gamma_risk: str         # 'NORMAL / THETA SAFE', 'DO NOT SELL PUTS', 'DO NOT SELL CALLS'


def rsi_wilder(series: pd.Series, period: int = 9) -> pd.Series:
    """Wilder's smoothed Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    
    # Avoid zero-division: where avg_loss == 0, RSI is 100 if gain > 0 else 50
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100.0 - (100.0 / (1.0 + rs))
    rsi = rsi.where(avg_loss != 0.0, np.where(avg_gain > 0, 100.0, 50.0))
    return rsi.fillna(50.0)


def wma_series(series: pd.Series, period: int = 21) -> pd.Series:
    """Linearly Weighted Moving Average (weights 1, 2, ..., period)."""
    weights = np.arange(1, period + 1)
    weight_sum = weights.sum()
    return series.rolling(period, min_periods=1).apply(
        lambda s: np.dot(s, weights[-len(s):]) / weights[-len(s):].sum(),
        raw=True,
    )


def compute_indicators(
    df: pd.DataFrame,
    *,
    ma_type: str = 'EMA',
    ma_len: int = 21,
    orb_minutes: int = 30,
    min_slope_pts: float = 0.50,
    max_extension_pts: float = 18.0,
    pullback_tol_pts: float = 1.5,
    pdh: Optional[float] = None,
    pdl: Optional[float] = None,
    pdc: Optional[float] = None,
    onh: Optional[float] = None,
    onl: Optional[float] = None,
) -> Tuple[pd.DataFrame, List[IndicatorSnapshot]]:
    """Compute the complete suite of DRMA, ORB, Hilega-Milega, and Confluence Scores.

    df must have columns ['datetime', 'open', 'high', 'low', 'close', 'is_today'].
    """
    if df.empty:
        return df, []

    df = df.copy().reset_index(drop=True)
    n = len(df)

    # 1. Hilega-Milega (calculated across continuous series for smooth zero-lag warm-up)
    rsi = rsi_wilder(df['close'], period=9)
    fast = rsi.ewm(span=3, adjust=False).mean()
    slow = wma_series(rsi, period=21)

    df['rsi'] = rsi
    df['hm_fast'] = fast
    df['hm_slow'] = slow

    # 2. Daily Reset Moving Average (resets at the first bar of 'today')
    out_ma_list: List[float] = []
    session_bar_idx = 0
    session_sum = 0.0
    session_ema = np.nan

    for i in range(n):
        row = df.iloc[i]
        is_today = bool(row.get('is_today', True))
        close_p = float(row['close'])

        # Reset session bar count when transitioning to today
        prev_is_today = bool(df.iloc[i - 1].get('is_today', False)) if i > 0 else False
        is_first_today_bar = is_today and not prev_is_today

        if is_first_today_bar or (i == 0 and is_today):
            session_bar_idx = 1
            session_sum = close_p
            session_ema = close_p
        elif is_today:
            session_bar_idx += 1
            session_sum += close_p
            eff_len = min(session_bar_idx, ma_len)
            alpha = 2.0 / (eff_len + 1.0)
            session_ema = (close_p * alpha) + (session_ema * (1.0 - alpha))
        else:
            # Warm-up / prior day bars: standard continuous MA
            eff_len = min(i + 1, ma_len)
            alpha = 2.0 / (eff_len + 1.0)
            session_ema = close_p if i == 0 else (close_p * alpha) + (session_ema * (1.0 - alpha))

        if ma_type.upper() == 'SMA':
            out_ma = session_sum / max(1, session_bar_idx) if is_today else close_p
        else:
            out_ma = session_ema
        out_ma_list.append(round(out_ma, 4))

    df['out_ma'] = out_ma_list

    # 3. 30-Minute ORB (Opening Range Breakout)
    # Starts at 08:30 CT (09:30 ET). Formed after 30 minutes (09:00 CT / 10:00 ET).
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    orb_mid: Optional[float] = None
    orb_cutoff_dt: Optional[pd.Timestamp] = None

    today_bars = df[df['is_today']].copy()
    if not today_bars.empty:
        # First bar timestamp determines RTH start
        first_dt_str = str(today_bars.iloc[0]['datetime'])
        try:
            start_dt = pd.to_datetime(first_dt_str)
            orb_cutoff_dt = start_dt + pd.Timedelta(minutes=orb_minutes)

            orb_slice = today_bars[pd.to_datetime(today_bars['datetime']) < orb_cutoff_dt]
            if not orb_slice.empty:
                orb_high = round(float(orb_slice['high'].max()), 2)
                orb_low = round(float(orb_slice['low'].min()), 2)
                orb_mid = round((orb_high + orb_low) / 2.0, 2)
        except Exception:
            orb_cutoff_dt = None

    snapshots: List[IndicatorSnapshot] = []

    # 4. Confluence Scoring & Snapshots
    for i in range(n):
        row = df.iloc[i]
        is_today = bool(row.get('is_today', True))
        bar_dt = pd.to_datetime(str(row['datetime']))
        bar_orb_formed = False
        if is_today and orb_cutoff_dt is not None:
            bar_orb_formed = (bar_dt >= orb_cutoff_dt)
        c = float(row['close'])
        o = float(row['open'])
        h = float(row['high'])
        l = float(row['low'])
        ma = float(row['out_ma'])

        prev_row = df.iloc[i - 1] if i > 0 else row
        prev2_row = df.iloc[i - 2] if i >= 2 else prev_row

        ma_delta_2 = round(ma - float(prev2_row['out_ma']), 4)
        ma_slope_up = ma_delta_2 >= min_slope_pts
        ma_slope_down = -ma_delta_2 >= min_slope_pts

        is_not_extended = abs(c - ma) <= max_extension_pts

        f_val = float(row['hm_fast'])
        s_val = float(row['hm_slow'])
        r_val = float(row['rsi'])

        is_super_bull = (f_val > s_val) and (f_val > 50.0) and (s_val > 50.0)
        is_super_bear = (f_val < s_val) and (f_val < 50.0) and (s_val < 50.0)
        is_bull_pullback = (s_val > 50.0) and (f_val < s_val)
        is_bear_bounce = (s_val < 50.0) and (f_val > s_val)

        # Bullish confluence (0-5)
        c_bull_level = (
            (orb_high is not None and c > orb_high)
            or (pdh is not None and c > pdh)
            or (onh is not None and c > onh)
            or (float(prev_row['close']) <= float(prev_row['out_ma']) and c > ma and c > (orb_mid or ma))
        )
        c_bull_ma = c > ma
        c_bull_slope = ma_slope_up
        c_bull_hm = is_super_bull
        c_bull_val = is_not_extended
        bull_score = sum([c_bull_level, c_bull_ma, c_bull_slope, c_bull_hm, c_bull_val])

        # Bearish confluence (0-5)
        c_bear_level = (
            (orb_low is not None and c < orb_low)
            or (pdl is not None and c < pdl)
            or (onl is not None and c < onl)
            or (float(prev_row['close']) >= float(prev_row['out_ma']) and c < ma and c < (orb_mid or ma))
        )
        c_bear_ma = c < ma
        c_bear_slope = ma_slope_down
        c_bear_hm = is_super_bear
        c_bear_val = is_not_extended
        bear_score = sum([c_bear_level, c_bear_ma, c_bear_slope, c_bear_hm, c_bear_val])

        # Bias & Grade
        is_bull_bias = bull_score >= 3 and bull_score > bear_score
        is_bear_bias = bear_score >= 3 and bear_score > bull_score

        if is_bull_bias:
            bias = 'BULL'
            active_score = bull_score
        elif is_bear_bias:
            bias = 'BEAR'
            active_score = bear_score
        else:
            bias = 'NEUTRAL'
            active_score = 0

        if active_score == 5:
            setup_grade = 'GRADE A+ (5/5)'
        elif active_score == 4:
            setup_grade = 'GRADE A (4/5)'
        elif active_score == 3:
            setup_grade = 'GRADE B (3/5)'
        else:
            setup_grade = 'CHOP / NO EDGE'

        # Trade vehicle recommendation from Pine Script matrix
        if bull_score == 5:
            trade_vehicle = 'BUY CALLS / CDS'
        elif bull_score == 4:
            trade_vehicle = 'SELL PCS'
        elif bull_score == 3:
            trade_vehicle = 'FAR OTM PCS'
        elif bear_score == 5:
            trade_vehicle = 'BUY PUTS / PDS'
        elif bear_score == 4:
            trade_vehicle = 'SELL CCS'
        elif bear_score == 3:
            trade_vehicle = 'FAR OTM CCS'
        else:
            if orb_high is not None and orb_low is not None and orb_low <= c <= orb_high:
                trade_vehicle = 'IRON CONDOR / CASH'
            else:
                trade_vehicle = 'STAY IN CASH'

        # 0DTE Gamma Guard Shield
        if bear_score >= 4 and r_val < 42.0:
            gamma_risk = 'DO NOT SELL PUTS'
        elif bull_score >= 4 and r_val > 58.0:
            gamma_risk = 'DO NOT SELL CALLS'
        else:
            gamma_risk = 'NORMAL / THETA SAFE'

        snap = IndicatorSnapshot(
            bar_index=i,
            datetime_str=str(row['datetime']),
            close=round(c, 2),
            open=round(o, 2),
            high=round(h, 2),
            low=round(l, 2),
            out_ma=round(ma, 2),
            ma_delta_2=round(ma_delta_2, 2),
            ma_slope_up=ma_slope_up,
            ma_slope_down=ma_slope_down,
            is_not_extended=is_not_extended,
            orb_high=orb_high,
            orb_low=orb_low,
            orb_mid=orb_mid,
            orb_formed=bar_orb_formed,
            pdh=pdh,
            pdl=pdl,
            pdc=pdc,
            onh=onh,
            onl=onl,
            rsi=round(r_val, 2),
            hm_fast=round(f_val, 2),
            hm_slow=round(s_val, 2),
            is_super_bull=is_super_bull,
            is_super_bear=is_super_bear,
            is_bull_pullback=is_bull_pullback,
            is_bear_bounce=is_bear_bounce,
            bull_score=bull_score,
            bear_score=bear_score,
            active_score=active_score,
            bias=bias,
            setup_grade=setup_grade,
            trade_vehicle=trade_vehicle,
            gamma_risk=gamma_risk,
        )
        snapshots.append(snap)

    return df, snapshots


def evaluate_exit_signal(
    current_side: str, # 'LONG' or 'SHORT'
    snapshots: List[IndicatorSnapshot],
    exit_style: str = 'trend_runner',
    min_hold_bars: int = 3,
    entry_bar_index: int = 0,
) -> Tuple[bool, str]:
    """Check if the position should exit based on momentum or trend exhaustion.

    Returns (should_exit, reason).
    """
    if not snapshots or current_side not in ('LONG', 'SHORT'):
        return False, ''

    curr = snapshots[-1]
    bars_held = curr.bar_index - entry_bar_index
    if bars_held < min_hold_bars:
        return False, f'holding (bar {bars_held}/{min_hold_bars})'

    prev = snapshots[-2] if len(snapshots) >= 2 else curr

    # Crossover of Fast/Slow
    cross_under_hm = (prev.hm_fast >= prev.hm_slow) and (curr.hm_fast < curr.hm_slow)
    cross_over_hm = (prev.hm_fast <= prev.hm_slow) and (curr.hm_fast > curr.hm_slow)

    if current_side == 'LONG':
        if exit_style == 'trend_runner':
            # 2-bar close below MA, or HM cross under with RSI submerged < 45
            if (curr.close < curr.out_ma and prev.close < prev.out_ma):
                return True, '2-bar close below 21 EMA'
            if cross_under_hm and curr.rsi < 45.0:
                return True, 'H-M Fast crossed under Slow with RSI < 45'
        elif exit_style == 'confirmed_hm':
            if (cross_under_hm and curr.rsi < 50.0) or (curr.close < curr.out_ma):
                return True, 'Confirmed H-M reversal below 50 / lost 21 EMA'
        else:  # tight_scalp
            if cross_under_hm or (curr.close < curr.out_ma):
                return True, 'Micro-hook fast exit'

    elif current_side == 'SHORT':
        if exit_style == 'trend_runner':
            # 2-bar close above MA, or HM cross over with RSI above > 55
            if (curr.close > curr.out_ma and prev.close > prev.out_ma):
                return True, '2-bar close above 21 EMA'
            if cross_over_hm and curr.rsi > 55.0:
                return True, 'H-M Fast crossed over Slow with RSI > 55'
        elif exit_style == 'confirmed_hm':
            if (cross_over_hm and curr.rsi > 50.0) or (curr.close > curr.out_ma):
                return True, 'Confirmed H-M reversal above 50 / reclaimed 21 EMA'
        else:  # tight_scalp
            if cross_over_hm or (curr.close > curr.out_ma):
                return True, 'Micro-hook fast exit'

    return False, ''
