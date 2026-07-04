"""Fetch options chain data for GEX calculation via TastyTrade."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from gex_calc import (
    CONTRACT_MULTIPLIER,
    build_gex_result,
    build_heatmap_result,
    compute_gamma_bs,
    gex_per_contract,
    tte_years_from_expiry,
)

log = logging.getLogger(__name__)

INDEX_TICKERS = frozenset({'SPX', 'NDX', 'RUT', 'VIX', 'DJX'})
GREEKS_TIMEOUT_SEC = 10.0
GREEKS_POLL_SEC = 0.3
DEFAULT_STRIKE_PCT = 5.0
MAX_HEATMAP_EXPIRIES = 16
DEFAULT_IV = 0.15


async def list_expirations(session, ticker: str) -> List[str]:
    from tastytrade.instruments import get_option_chain

    chain = await get_option_chain(session, ticker.upper())
    return sorted(d.isoformat() for d in chain.keys())


async def _fetch_spot(session, ticker: str) -> float:
    from tastytrade.market_data import get_market_data
    from tastytrade.order import InstrumentType

    sym = ticker.upper()
    if sym in INDEX_TICKERS:
        md = await get_market_data(session, sym, InstrumentType.INDEX)
    else:
        md = await get_market_data(session, sym, InstrumentType.EQUITY)

    for attr in ('mid', 'mark', 'last'):
        val = getattr(md, attr, None)
        if val is not None and float(val) > 0:
            return float(val)
    bid, ask = getattr(md, 'bid', None), getattr(md, 'ask', None)
    if bid and ask and float(bid) > 0 and float(ask) > 0:
        return (float(bid) + float(ask)) / 2.0
    raise ValueError(f'No live price for {sym}')


async def _fetch_open_interest(session, symbols: List[str]) -> Dict[str, int]:
    from tastytrade.market_data import get_market_data_by_type

    oi_map: Dict[str, int] = {}
    for i in range(0, len(symbols), 100):
        batch = symbols[i:i + 100]
        try:
            items = await get_market_data_by_type(session, options=batch)
        except Exception as exc:
            log.warning('OI batch failed: %s', exc)
            continue
        for md in items:
            sym = getattr(md, 'symbol', '') or ''
            oi = getattr(md, 'open_interest', None)
            if sym and oi is not None:
                oi_map[sym] = int(oi)
        if i + 100 < len(symbols):
            await asyncio.sleep(0.2)
    return oi_map


async def _fetch_greeks(session, streamer_symbols: List[str]) -> Dict[str, Tuple[float, float]]:
    """Return {streamer_symbol: (gamma, iv)} from DXLink Greeks feed."""
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Greeks

    if not streamer_symbols:
        return {}

    result: Dict[str, Tuple[float, float]] = {}
    target = set(streamer_symbols)
    deadline = time.time() + GREEKS_TIMEOUT_SEC

    try:
        async with DXLinkStreamer(session) as streamer:
            await streamer.subscribe(Greeks, streamer_symbols, refresh_interval=1.0)
            while time.time() < deadline and len(result) < len(target):
                try:
                    event = await asyncio.wait_for(streamer.get_event(Greeks), timeout=GREEKS_POLL_SEC)
                except asyncio.TimeoutError:
                    continue
                sym = getattr(event, 'event_symbol', '') or ''
                gamma = getattr(event, 'gamma', None)
                iv = getattr(event, 'volatility', None)
                if sym and gamma is not None:
                    result[sym] = (float(gamma), float(iv) if iv else 0.0)
    except Exception as exc:
        log.warning('Greeks stream failed (%s); will fall back to Black-Scholes', exc)

    return result


def _filter_options(options, expiry: date, spot: float, strike_pct: float):
    lo = spot * (1 - strike_pct / 100)
    hi = spot * (1 + strike_pct / 100)
    filtered = []
    for opt in options:
        if opt.expiration_date != expiry:
            continue
        strike = float(opt.strike_price)
        if strike < lo or strike > hi:
            continue
        filtered.append(opt)
    return filtered


async def fetch_gex_async(
    session,
    ticker: str,
    expiry: date,
    *,
    strike_pct: float = DEFAULT_STRIKE_PCT,
) -> Dict[str, Any]:
    from tastytrade.instruments import OptionType, get_option_chain

    sym = ticker.upper()
    spot = await _fetch_spot(session, sym)
    chain = await get_option_chain(session, sym)
    options = chain.get(expiry, [])
    if not options:
        raise ValueError(f'No options for {sym} expiring {expiry.isoformat()}')

    filtered = _filter_options(options, expiry, spot, strike_pct)
    if not filtered:
        raise ValueError(f'No strikes within ±{strike_pct}% of spot ({spot:.2f})')

    occ_symbols = [opt.symbol for opt in filtered]
    streamer_symbols = [opt.streamer_symbol for opt in filtered if opt.streamer_symbol]

    oi_task = asyncio.create_task(_fetch_open_interest(session, occ_symbols))
    greeks_task = asyncio.create_task(_fetch_greeks(session, streamer_symbols))
    oi_map, greeks_map = await asyncio.gather(oi_task, greeks_task)

    tte = tte_years_from_expiry(
        expiry,
        expires_at=filtered[0].expires_at if filtered else None,
    )
    default_iv = 0.15
    if greeks_map:
        ivs = [v[1] for v in greeks_map.values() if v[1] > 0]
        if ivs:
            default_iv = sum(ivs) / len(ivs)

    rows: List[Dict[str, Any]] = []
    for opt in filtered:
        oi = oi_map.get(opt.symbol, 0)
        if oi <= 0:
            continue
        is_call = opt.option_type == OptionType.CALL
        ss = opt.streamer_symbol
        if ss in greeks_map:
            gamma, iv = greeks_map[ss]
        else:
            iv = default_iv
            gamma = compute_gamma_bs(spot, float(opt.strike_price), tte, iv)

        gex = gex_per_contract(oi, gamma, spot, is_call)
        rows.append({
            'strike': float(opt.strike_price),
            'option_type': 'call' if is_call else 'put',
            'open_interest': oi,
            'gamma': gamma,
            'iv': iv,
            'gex': gex,
            'symbol': opt.symbol,
        })

    if not rows:
        raise ValueError('No contracts with open interest in selected range')

    multiplier = int(getattr(filtered[0], 'shares_per_contract', None) or CONTRACT_MULTIPLIER)
    return build_gex_result(rows, spot, sym, expiry, multiplier=multiplier)


def fetch_gex(broker, ticker: str, expiry: date, strike_pct: float = DEFAULT_STRIKE_PCT) -> Dict[str, Any]:
    """Sync entry point — uses broker's background event loop."""
    return broker._run(fetch_gex_async(broker.session, ticker, expiry, strike_pct=strike_pct))


async def fetch_gex_heatmap_async(
    session,
    ticker: str,
    *,
    strike_pct: float = DEFAULT_STRIKE_PCT,
    max_expiries: int = MAX_HEATMAP_EXPIRIES,
) -> Dict[str, Any]:
    """Multi-expiry GEX grid — Black-Scholes gamma only (no live Greeks stream)."""
    from tastytrade.instruments import OptionType, get_option_chain
    from meic0dte.app.utilities import central_now

    sym = ticker.upper()
    spot = await _fetch_spot(session, sym)
    chain = await get_option_chain(session, sym)
    today = central_now().date()

    expiries = sorted(d for d in chain.keys() if d >= today)[:max_expiries]
    if not expiries:
        raise ValueError(f'No upcoming expirations for {sym}')

    lo = spot * (1 - strike_pct / 100)
    hi = spot * (1 + strike_pct / 100)

    all_opts = []
    for exp in expiries:
        for opt in chain.get(exp, []):
            strike = float(opt.strike_price)
            if lo <= strike <= hi:
                all_opts.append(opt)

    if not all_opts:
        raise ValueError(f'No strikes within ±{strike_pct}% of spot ({spot:.2f})')

    oi_map = await _fetch_open_interest(session, [o.symbol for o in all_opts])

    matrix: Dict[tuple, float] = {}
    for exp in expiries:
        exp_opts = [o for o in all_opts if o.expiration_date == exp]
        if not exp_opts:
            continue
        tte = tte_years_from_expiry(exp, expires_at=exp_opts[0].expires_at)
        exp_key = exp.isoformat()
        for opt in exp_opts:
            oi = oi_map.get(opt.symbol, 0)
            if oi <= 0:
                continue
            strike = float(opt.strike_price)
            is_call = opt.option_type == OptionType.CALL
            gamma = compute_gamma_bs(spot, strike, tte, DEFAULT_IV)
            gex = gex_per_contract(oi, gamma, spot, is_call)
            key = (strike, exp_key)
            matrix[key] = matrix.get(key, 0.0) + gex

    if not matrix:
        raise ValueError('No contracts with open interest in selected range')

    strikes = sorted({s for s, _ in matrix.keys()}, reverse=True)
    expiry_strs = [e.isoformat() for e in expiries]
    return build_heatmap_result(matrix, strikes, expiry_strs, spot, sym)


def fetch_gex_heatmap(
    broker,
    ticker: str,
    strike_pct: float = DEFAULT_STRIKE_PCT,
    max_expiries: int = MAX_HEATMAP_EXPIRIES,
) -> Dict[str, Any]:
    return broker._run(
        fetch_gex_heatmap_async(
            broker.session, ticker, strike_pct=strike_pct, max_expiries=max_expiries
        )
    )
