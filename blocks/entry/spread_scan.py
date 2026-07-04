"""Credit-spread strike scanner — shared by MEIC entry and Manual Spread.

Canonical location: ``blocks/entry/spread_scan.py`` (V2 entry block).
``meic0dte.open.spread_scan`` re-exports for backward compatibility.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

import meic0dte.app.config as config
from common.mqtt_prices import register_symbols_and_wait
from common.strike_guard import leg_overlap_conflict, resolve_leg_overlap
from common.symbols import build_schwab_symbol, build_tastytrade_symbol, to_tastytrade


@dataclass
class SpreadCandidate:
    short_symbol: str
    long_symbol: str
    short_strike: int
    long_strike: int
    market_credit: float
    short_mid: float
    long_mid: float
    distance_from_target: float = 0.0
    overlap_warning: Optional[str] = None
    overlap_shifts: int = 0


# When overlap shift lands outside session credit band, relax min/max by this fraction.
OVERLAP_SHIFT_CREDIT_BAND_EXPANSION = 0.10


def _overlap_shift_credit_bounds(
    credit_min: Optional[float],
    credit_max: Optional[float],
    *,
    expansion: float = OVERLAP_SHIFT_CREDIT_BAND_EXPANSION,
) -> Tuple[Optional[float], Optional[float]]:
    """Widen credit band for overlap-shift evaluation only (e.g. 0.60–1.00 → 0.54–1.10)."""
    eff_min = credit_min * (1.0 - expansion) if credit_min is not None else None
    eff_max = credit_max * (1.0 + expansion) if credit_max is not None else None
    return eff_min, eff_max


def _candidate_preference_key(candidate: SpreadCandidate) -> Tuple:
    """Lower is better: clean overlap first, then nearer target credit."""
    return (1 if candidate.overlap_warning else 0, candidate.distance_from_target)


def _dedupe_spread_candidates(candidates: List[SpreadCandidate]) -> List[SpreadCandidate]:
    """Drop duplicate strike pairs (shifted spread can match a later OTM hit)."""
    best: Dict[Tuple[int, int], SpreadCandidate] = {}
    order: List[Tuple[int, int]] = []
    for candidate in candidates:
        key = (candidate.short_strike, candidate.long_strike)
        if key not in best:
            best[key] = candidate
            order.append(key)
            continue
        if _candidate_preference_key(candidate) < _candidate_preference_key(best[key]):
            best[key] = candidate
    return [best[key] for key in order]


def _resolve_overlap_candidate(
    broker,
    candidate: SpreadCandidate,
    opt_type: str,
    expiry: str,
    log,
    *,
    quote_source: str,
    price_map: Optional[Dict[str, float]],
    target_credit: Optional[float],
    credit_min: Optional[float],
    credit_max: Optional[float],
    min_market_credit: float,
    exclude_path: Optional[str] = None,
) -> SpreadCandidate:
    """Shift strikes $5 (CCS down / PCS up) until leg flip conflict clears."""
    if not candidate.overlap_warning:
        return candidate

    resolved = resolve_leg_overlap(
        expiry,
        opt_type,
        candidate.short_strike,
        candidate.long_strike,
        exclude_path=exclude_path,
    )
    if resolved is None:
        return candidate

    ss, ls, short_sym, long_sym, shifts = resolved
    if shifts == 0:
        return candidate

    if quote_source == 'api':
        extra = broker.fetch_option_mids_api([short_sym, long_sym])
        merged = dict(price_map or {})
        merged.update(extra)
        price_map = merged

    short_p, long_p = _leg_prices(broker, short_sym, long_sym, quote_source, price_map)
    if short_p is None or long_p is None:
        log.warning(
            'Overlap shift %s/%s -> %s/%s: missing quotes — keeping original strikes',
            candidate.short_strike,
            candidate.long_strike,
            ss,
            ls,
        )
        return candidate

    shift_credit_min, shift_credit_max = _overlap_shift_credit_bounds(credit_min, credit_max)
    adjusted = _evaluate_spread(
        short_symbol=short_sym,
        long_symbol=long_sym,
        short_strike=ss,
        long_strike=ls,
        short_p=short_p,
        long_p=long_p,
        opt_type=opt_type,
        target_credit=target_credit,
        credit_min=shift_credit_min,
        credit_max=shift_credit_max,
        min_market_credit=min_market_credit,
        check_overlap=True,
    )
    if adjusted is None:
        log.warning(
            'Overlap shift %s/%s -> %s/%s: out of credit band after shift (even with %.0f%% expansion)',
            candidate.short_strike,
            candidate.long_strike,
            ss,
            ls,
            OVERLAP_SHIFT_CREDIT_BAND_EXPANSION * 100,
        )
        return candidate
    if adjusted.overlap_warning:
        log.warning(
            'Overlap shift %s/%s -> %s/%s still has leg overlap',
            candidate.short_strike,
            candidate.long_strike,
            ss,
            ls,
        )
        return candidate

    adjusted.overlap_shifts = shifts
    log.info(
        'Shifted %s spread %s/%s by $%d x%d -> %s/%s (credit %.2f) to avoid leg overlap',
        opt_type,
        candidate.short_strike,
        candidate.long_strike,
        5,
        shifts,
        ss,
        ls,
        adjusted.market_credit,
    )
    return adjusted


def _otm_values(otm_min: int, otm_max: int, step: int) -> range:
    """Inclusive OTM grid (range(5,150,5) omits 150 — we need the endpoint)."""
    return range(otm_min, otm_max + 1, step)


def scan_symbol_list(
    expiry: str,
    opt_type: str,
    spx_price: int,
    *,
    spread_width: int,
    otm_min: int,
    otm_max: int,
    step: int = config.STEP,
) -> list[str]:
    symbols: list[str] = []
    for otm in _otm_values(otm_min, otm_max, step):
        if opt_type == 'C':
            short_strike = int(spx_price + otm)
            long_strike = int(spx_price + otm + spread_width)
        else:
            short_strike = int(spx_price - otm)
            long_strike = int(spx_price - otm - spread_width)
        symbols.append(build_schwab_symbol(expiry, opt_type, short_strike))
        symbols.append(build_schwab_symbol(expiry, opt_type, long_strike))
    return list(dict.fromkeys(symbols))


def _register_scan_symbols(
    broker,
    expiry: str,
    opt_type: str,
    spx_price: int,
    lot: str,
    log,
    *,
    spread_widths: list[int],
    otm_min: int,
    otm_max: int,
) -> None:
    symbols: list[str] = []
    for width in spread_widths:
        symbols.extend(
            scan_symbol_list(
                expiry, opt_type, spx_price,
                spread_width=width, otm_min=otm_min, otm_max=otm_max,
            )
        )
    register_symbols_and_wait(list(dict.fromkeys(symbols)), lot, log)


def _fetch_option_mids_robust(
    broker,
    symbols: List[str],
    log,
    *,
    chunk_size: int = 40,
) -> Dict[str, float]:
    """Fetch option mids in chunks; retry missing symbols in smaller batches."""
    from common.symbols import to_tastytrade

    unique = list(dict.fromkeys(symbols))
    out: Dict[str, float] = {}
    for i in range(0, len(unique), chunk_size):
        batch = unique[i:i + chunk_size]
        out.update(broker.fetch_option_mids_api(batch))

    missing = [sym for sym in unique if to_tastytrade(sym) not in out]
    if missing:
        for i in range(0, len(missing), 10):
            retry_batch = missing[i:i + 10]
            extra = broker.fetch_option_mids_api(retry_batch)
            out.update(extra)

    got = len({to_tastytrade(s) for s in unique if to_tastytrade(s) in out})
    log.info('API quotes received for %d/%d symbols', got, len(unique))
    if got < len(unique) * 0.5:
        log.warning(
            'Low quote coverage (%d/%d) — scan may skew toward strikes with REST data',
            got, len(unique),
        )
    return out


# OTM sweep limits — low credits need strikes further from SPX (manual + MEIC).
OTM_MAX_DEFAULT = 150
OTM_MAX_MODERATE = 250
OTM_MAX_DEEP = 300


def resolve_scan_otm_max(
    *,
    target_credit: Optional[float] = None,
    credit_min: Optional[float] = None,
    default: int = OTM_MAX_DEFAULT,
) -> int:
    """Extend OTM range when hunting low credits (shared by Manual scan + MEIC entry)."""
    anchor = target_credit if target_credit is not None else credit_min
    if anchor is None:
        return default
    if anchor <= 0.70:
        return OTM_MAX_DEEP
    if anchor <= 0.90:
        return OTM_MAX_MODERATE
    if anchor <= 1.20:
        return max(default, 200)
    return max(default, OTM_MAX_DEFAULT)


def _round_credit(raw_credit: float) -> float:
    rounded = round(raw_credit / 0.05) * 0.05
    if rounded > raw_credit:
        rounded -= config.OPEN_PRICE_ADJ
    return round(rounded, 2)


def _iter_spread_legs(
    widths: list[int],
    opt_type: str,
    spx_price: int,
    expiry: str,
    otm_min: int,
    otm_max: int,
) -> Iterable[Tuple[int, int, int, str, str]]:
    for width in widths:
        for otm in _otm_values(otm_min, otm_max, config.STEP):
            if opt_type == 'C':
                short_strike = int(spx_price + otm)
                long_strike = int(spx_price + otm + width)
            else:
                short_strike = int(spx_price - otm)
                long_strike = int(spx_price - otm - width)
            short_symbol = build_tastytrade_symbol(expiry, opt_type, short_strike)
            long_symbol = build_tastytrade_symbol(expiry, opt_type, long_strike)
            yield short_strike, long_strike, width, short_symbol, long_symbol


def _resolve_spx_price(broker, quote_source: str, log) -> int:
    if quote_source == 'api':
        spx = broker.fetch_spx_price_api()
        source = 'api'
        if spx is None:
            log.warning(
                'SPX REST market-data empty (after hours, rate limit, or TT API gap) '
                '— falling back to MQTT streamer'
            )
            spx = broker.get_spx_price()
            source = 'mqtt-fallback'
        if spx is None:
            raise RuntimeError(
                'Could not get SPX price: REST returned no data and MQTT has no SPX '
                '(is publish_tastytrade.py running?)'
            )
        log.info('SPX price (%s): %s', source, spx)
    else:
        spx = broker.get_spx_price()
        if spx is None:
            raise RuntimeError(
                'Could not get SPX from MQTT streamer (is publish_tastytrade.py running?)'
            )
        log.info('SPX price (MQTT): %s', spx)
    return round(spx / 5) * 5


def _leg_prices(
    broker,
    short_symbol: str,
    long_symbol: str,
    quote_source: str,
    price_map: Optional[Dict[str, float]],
) -> Tuple[Optional[float], Optional[float]]:
    if quote_source == 'api':
        assert price_map is not None
        short_p = price_map.get(to_tastytrade(short_symbol))
        long_p = price_map.get(to_tastytrade(long_symbol))
        return short_p, long_p
    short_p = broker.get_option_price(short_symbol, timeout=2.0)
    long_p = broker.get_option_price(long_symbol, timeout=2.0)
    return short_p, long_p


def _evaluate_spread(
    *,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    short_p: float,
    long_p: float,
    opt_type: str,
    target_credit: Optional[float],
    credit_min: Optional[float],
    credit_max: Optional[float],
    min_market_credit: float,
    check_overlap: bool,
) -> Optional[SpreadCandidate]:
    raw_credit = short_p - long_p
    if raw_credit < min_market_credit:
        return None

    spread_credit = _round_credit(raw_credit)
    if spread_credit < min_market_credit:
        return None

    if target_credit is None:
        cmin = credit_min if credit_min is not None else config.CREDIT_MIN
        cmax = credit_max if credit_max is not None else (
            config.CREDIT_MAX_P if opt_type == 'P' else config.CREDIT_MAX_C
        )
        if not (cmin <= spread_credit <= cmax):
            return None

    overlap_warning = None
    if check_overlap:
        conflict = leg_overlap_conflict(short_symbol, long_symbol, opt_type)
        if conflict:
            overlap_warning = conflict

    distance = abs(spread_credit - target_credit) if target_credit is not None else 0.0
    return SpreadCandidate(
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        short_strike=short_strike,
        long_strike=long_strike,
        market_credit=spread_credit,
        short_mid=short_p,
        long_mid=long_p,
        distance_from_target=distance,
        overlap_warning=overlap_warning,
    )


def pick_meic_candidate(candidates: List[SpreadCandidate]) -> Optional[SpreadCandidate]:
    """First in-band candidate without overlap; None if list empty."""
    for candidate in candidates:
        if not candidate.overlap_warning:
            return candidate
    return None


def scan_credit_spreads(
    broker,
    opt_type: str,
    expiry: str,
    lot: str,
    log,
    *,
    spread_width: Optional[int] = None,
    spread_width_min: Optional[int] = None,
    spread_width_max: Optional[int] = None,
    otm_min: int = config.OTM_MIN,
    otm_max: int = config.OTM_MAX,
    credit_min: Optional[float] = None,
    credit_max: Optional[float] = None,
    target_credit: Optional[float] = None,
    min_market_credit: float = 0.05,
    max_results: int = 1,
    check_overlap: bool = True,
    quote_source: str = 'api',
) -> List[SpreadCandidate]:
    """Scan OTM grid and return spread candidates.

    quote_source='api' (default): TastyTrade REST market data — no streamer/MQTT.
    quote_source='mqtt': legacy path via optsymbols.json + MQTT mids.

    MEIC mode: width range + credit band, first match (max_results=1).
    Manual mode: fixed width + target_credit ranking (max_results=N).
    """
    opt_type = opt_type.upper()
    spx_price = _resolve_spx_price(broker, quote_source, log)

    if spread_width is not None:
        widths = [int(spread_width)]
    else:
        wmin = spread_width_min if spread_width_min is not None else config.SPREAD_WIDTH_MIN
        wmax = spread_width_max if spread_width_max is not None else config.SPREAD_WIDTH_MAX
        widths = list(range(wmin, wmax + 1, config.STEP))

    legs = list(_iter_spread_legs(widths, opt_type, spx_price, expiry, otm_min, otm_max))
    price_map: Optional[Dict[str, float]] = None

    if quote_source == 'mqtt':
        _register_scan_symbols(
            broker, expiry, opt_type, spx_price, lot, log,
            spread_widths=widths, otm_min=otm_min, otm_max=otm_max,
        )
    else:
        symbols = [sym for _, _, _, short_sym, long_sym in legs for sym in (short_sym, long_sym)]
        price_map = _fetch_option_mids_robust(broker, symbols, log)

    candidates: List[SpreadCandidate] = []
    for short_strike, long_strike, width, short_symbol, long_symbol in legs:
        short_p, long_p = _leg_prices(broker, short_symbol, long_symbol, quote_source, price_map)
        if short_p is None or long_p is None:
            if target_credit is None and credit_min is not None:
                raw_try = None
                if short_p is not None and long_p is not None:
                    raw_try = short_p - long_p
                if raw_try is not None and raw_try < (credit_min or config.CREDIT_MIN):
                    break
            continue

        candidate = _evaluate_spread(
            short_symbol=short_symbol,
            long_symbol=long_symbol,
            short_strike=short_strike,
            long_strike=long_strike,
            short_p=short_p,
            long_p=long_p,
            opt_type=opt_type,
            target_credit=target_credit,
            credit_min=credit_min,
            credit_max=credit_max,
            min_market_credit=min_market_credit,
            check_overlap=check_overlap,
        )
        if candidate is None:
            if target_credit is None and credit_min is not None and (short_p - long_p) < credit_min:
                break
            continue

        candidate = _resolve_overlap_candidate(
            broker,
            candidate,
            opt_type,
            expiry,
            log,
            quote_source=quote_source,
            price_map=price_map,
            target_credit=target_credit,
            credit_min=credit_min,
            credit_max=credit_max,
            min_market_credit=min_market_credit,
        )

        log.info(
            '%s - %.2f | %s - %.2f = %.2f',
            candidate.short_symbol, candidate.short_mid, candidate.long_symbol,
            candidate.long_mid, candidate.market_credit,
        )
        candidates.append(candidate)

        if target_credit is None and max_results == 1 and not candidate.overlap_warning:
            return [candidate]

    if target_credit is not None:
        candidates = _dedupe_spread_candidates(candidates)
        candidates.sort(key=lambda c: c.distance_from_target)
        trimmed = candidates[:max_results]
        if trimmed and trimmed[0].distance_from_target > 0.35:
            log.warning(
                'Nearest candidate is %.2f from target $%.2f (best credit %.2f) — '
                'try wider OTM or check API quote coverage',
                trimmed[0].distance_from_target,
                target_credit,
                trimmed[0].market_credit,
            )
        return trimmed

    candidates = _dedupe_spread_candidates(candidates)

    clean = pick_meic_candidate(candidates)
    if clean is not None:
        return [clean]

    if not candidates:
        return []

    for candidate in candidates:
        if candidate.overlap_warning:
            resolved = _resolve_overlap_candidate(
                broker,
                candidate,
                opt_type,
                expiry,
                log,
                quote_source=quote_source,
                price_map=price_map,
                target_credit=target_credit,
                credit_min=credit_min,
                credit_max=credit_max,
                min_market_credit=min_market_credit,
            )
            if not resolved.overlap_warning:
                return [resolved]
    return candidates[:max_results]
