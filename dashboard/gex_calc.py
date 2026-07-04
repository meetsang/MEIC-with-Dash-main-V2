"""Gamma Exposure (GEX) calculation — per GEX_Calculation_Guide."""
from __future__ import annotations

import math
from datetime import date, datetime, time, timezone
from typing import Any, Dict, List, Optional


RISK_FREE_RATE = 0.05
CONTRACT_MULTIPLIER = 100


def norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def compute_gamma_bs(spot: float, strike: float, tte_years: float, iv: float,
                     r: float = RISK_FREE_RATE) -> float:
    """Black-Scholes gamma when feed does not supply it."""
    if tte_years <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return 0.0
    d1 = (math.log(spot / strike) + (r + 0.5 * iv ** 2) * tte_years) / (iv * math.sqrt(tte_years))
    return norm_pdf(d1) / (spot * iv * math.sqrt(tte_years))


def tte_years_from_expiry(expiry: date, expires_at: Optional[datetime] = None,
                          now: Optional[datetime] = None) -> float:
    """Time to expiration in years (trading-hours basis for 0DTE)."""
    now = now or datetime.now(timezone.utc)
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        seconds = max((expires_at - now).total_seconds(), 60.0)
    else:
        end = datetime.combine(expiry, time(21, 0), tzinfo=timezone.utc)  # ~4pm ET
        seconds = max((end - now).total_seconds(), 60.0)
    return seconds / (252 * 6.5 * 3600)


def gex_per_contract(oi: float, gamma: float, spot: float, is_call: bool,
                     multiplier: int = CONTRACT_MULTIPLIER) -> float:
    sign = 1.0 if is_call else -1.0
    return oi * gamma * spot * multiplier * sign


def aggregate_by_strike(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Aggregate call/put GEX into per-strike net GEX."""
    by_strike: Dict[float, Dict[str, Any]] = {}
    for row in rows:
        strike = float(row['strike'])
        gex = float(row['gex'])
        oi = int(row.get('open_interest') or 0)
        is_call = row['option_type'] == 'call'
        bucket = by_strike.setdefault(strike, {
            'strike': strike,
            'call_gex': 0.0,
            'put_gex': 0.0,
            'net_gex': 0.0,
            'call_oi': 0,
            'put_oi': 0,
        })
        if is_call:
            bucket['call_gex'] += gex
            bucket['call_oi'] += oi
        else:
            bucket['put_gex'] += gex
            bucket['put_oi'] += oi
        bucket['net_gex'] += gex

    result = sorted(by_strike.values(), key=lambda x: x['strike'])
    for row in result:
        row['cumulative'] = 0.0
    return result


def apply_cumulative(strikes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    total = 0.0
    for row in strikes:
        total += row['net_gex']
        row['cumulative'] = total
    return strikes


def find_key_levels(strikes: List[Dict[str, Any]], spot: float) -> Dict[str, Any]:
    """Call wall, put wall, gamma flip, and distances from spot."""
    if not strikes:
        return {
            'call_wall': None,
            'put_wall': None,
            'gamma_flip': None,
            'call_wall_dist': None,
            'put_wall_dist': None,
            'gamma_flip_dist': None,
        }

    pos = [s for s in strikes if s['net_gex'] > 0]
    neg = [s for s in strikes if s['net_gex'] < 0]
    call_wall = max(pos, key=lambda s: s['net_gex'])['strike'] if pos else None
    put_wall = min(neg, key=lambda s: s['net_gex'])['strike'] if neg else None

    gamma_flip = None
    sorted_strikes = sorted(strikes, key=lambda s: s['strike'])
    for i, row in enumerate(sorted_strikes):
        cum = row.get('cumulative', 0.0)
        if cum >= 0:
            if i == 0:
                gamma_flip = row['strike']
            else:
                prev = sorted_strikes[i - 1]
                if abs(prev['cumulative']) <= abs(cum):
                    gamma_flip = prev['strike']
                else:
                    gamma_flip = row['strike']
            break
    if gamma_flip is None and sorted_strikes:
        gamma_flip = sorted_strikes[-1]['strike']

    def _dist(level: Optional[float]) -> Optional[float]:
        return round(level - spot, 1) if level is not None else None

    return {
        'call_wall': call_wall,
        'put_wall': put_wall,
        'gamma_flip': gamma_flip,
        'call_wall_dist': _dist(call_wall),
        'put_wall_dist': _dist(put_wall),
        'gamma_flip_dist': _dist(gamma_flip),
    }


def build_gex_result(
    rows: List[Dict[str, Any]],
    spot: float,
    ticker: str,
    expiry: date,
    *,
    multiplier: int = CONTRACT_MULTIPLIER,
) -> Dict[str, Any]:
    """Full GEX payload for API / dashboard."""
    strikes = apply_cumulative(aggregate_by_strike(rows))
    net_gex = sum(s['net_gex'] for s in strikes)
    levels = find_key_levels(strikes, spot)
    regime = 'positive' if net_gex >= 0 else 'negative'

    return {
        'ticker': ticker,
        'expiry': expiry.isoformat(),
        'spot': round(spot, 2),
        'multiplier': multiplier,
        'net_gex': net_gex,
        'net_gex_fmt': _fmt_dollars(net_gex),
        'regime': regime,
        'regime_label': 'Positive Gamma' if regime == 'positive' else 'Negative Gamma',
        'levels': levels,
        'strikes': strikes,
        'contract_count': len(rows),
        'strike_count': len(strikes),
    }


def _fmt_dollars(val: float) -> str:
    sign = '+' if val >= 0 else ''
    av = abs(val)
    if av >= 1e9:
        return f'{sign}${av / 1e9:.2f}B'
    if av >= 1e6:
        return f'{sign}${av / 1e6:.1f}M'
    if av >= 1e3:
        return f'{sign}${av / 1e3:.0f}K'
    return f'{sign}${av:.0f}'


def fmt_compact(val: float) -> str:
    """Compact dollar label for heatmap cells."""
    if val == 0:
        return '0'
    sign = '-' if val < 0 else ''
    av = abs(val)
    if av >= 1e9:
        return f'{sign}{av / 1e9:.1f}B'
    if av >= 1e6:
        return f'{sign}{av / 1e6:.1f}M'
    if av >= 1e3:
        return f'{sign}{av / 1e3:.1f}K'
    return f'{sign}{av:.0f}'


def build_heatmap_result(
    matrix: Dict[tuple, float],
    strikes: List[float],
    expiries: List[str],
    spot: float,
    ticker: str,
) -> Dict[str, Any]:
    """Build heatmap payload with summary stats."""
    cells: Dict[str, Dict[str, float]] = {}
    all_vals: List[float] = []
    for strike in strikes:
        row: Dict[str, float] = {}
        for exp in expiries:
            val = matrix.get((strike, exp), 0.0)
            row[exp] = val
            if val:
                all_vals.append(val)
        cells[str(int(strike) if strike == int(strike) else strike)] = row

    total_net = sum(matrix.values())
    by_strike = sorted(
        (
            {'strike': s, 'net_gex': sum(matrix.get((s, e), 0.0) for e in expiries)}
            for s in strikes
        ),
        key=lambda x: x['strike'],
    )
    cum = 0.0
    cumulative_strikes = []
    for row in by_strike:
        cum += row['net_gex']
        cumulative_strikes.append({
            'strike': row['strike'],
            'net_gex': row['net_gex'],
            'cumulative': cum,
        })
    levels = find_key_levels(cumulative_strikes, spot)

    max_pos = max_pos_strike = max_pos_exp = None
    max_neg = max_neg_strike = max_neg_exp = None
    for (strike, exp), val in matrix.items():
        if val > 0 and (max_pos is None or val > max_pos):
            max_pos, max_pos_strike, max_pos_exp = val, strike, exp
        if val < 0 and (max_neg is None or val < max_neg):
            max_neg, max_neg_strike, max_neg_exp = val, strike, exp

    gamma_slope = None
    if len(by_strike) >= 3:
        spot_idx = min(range(len(by_strike)), key=lambda i: abs(by_strike[i]['strike'] - spot))
        if 0 < spot_idx < len(by_strike) - 1:
            d_strike = by_strike[spot_idx + 1]['strike'] - by_strike[spot_idx - 1]['strike']
            d_gex = (
                by_strike[spot_idx + 1]['net_gex'] - by_strike[spot_idx - 1]['net_gex']
            )
            if d_strike:
                gamma_slope = d_gex / d_strike

    expiry_labels = []
    for exp in expiries:
        y, m, d = exp.split('-')
        months = 'JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC'.split()
        expiry_labels.append(f'{months[int(m) - 1]} {int(d)}')

    return {
        'ticker': ticker,
        'spot': round(spot, 2),
        'expiries': expiries,
        'expiry_labels': expiry_labels,
        'strikes': strikes,
        'cells': cells,
        'total_net_gex': total_net,
        'total_net_gex_fmt': _fmt_dollars(total_net),
        'gamma_flip': levels.get('gamma_flip'),
        'gamma_slope': gamma_slope,
        'gamma_slope_fmt': fmt_compact(gamma_slope) if gamma_slope is not None else None,
        'max_pos': {
            'gex': max_pos,
            'gex_fmt': _fmt_dollars(max_pos) if max_pos else None,
            'strike': max_pos_strike,
            'expiry': max_pos_exp,
        },
        'max_neg': {
            'gex': max_neg,
            'gex_fmt': _fmt_dollars(max_neg) if max_neg else None,
            'strike': max_neg_strike,
            'expiry': max_neg_exp,
        },
        'max_abs': max((abs(v) for v in all_vals), default=0),
    }
