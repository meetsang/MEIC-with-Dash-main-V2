"""TastyTrade market-data helpers. Read-only — no order endpoints."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from mnq_paper.models import LiveQuote

log = logging.getLogger('mnq_paper.quotes')


@dataclass
class FutureContract:
    product_code: str
    symbol: str
    streamer_symbol: str
    expiration_date: str


def _mid(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is not None and ask is not None and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None


def _f(val) -> Optional[float]:
    if val is None:
        return None
    try:
        out = float(val)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


async def resolve_contract(session, product_code: str, rest_symbol: str) -> FutureContract:
    from tastytrade.instruments import Future

    try:
        contract = await Future.get(session, rest_symbol)
        if not isinstance(contract, list):
            streamer = (contract.streamer_symbol or '').strip()
            if not streamer:
                streamer = rest_symbol
            return FutureContract(
                product_code=product_code,
                symbol=contract.symbol,
                streamer_symbol=streamer,
                expiration_date=str(contract.expiration_date),
            )
    except Exception as exc:
        log.warning('Future.get(%s) failed (%s) — falling back to product scan', rest_symbol, exc)

    futures = await Future.get(session, product_codes=[product_code])
    if not isinstance(futures, list):
        futures = [futures]
    active = [f for f in futures if getattr(f, 'active', False)]
    if not active:
        raise RuntimeError(f'No active {product_code} futures from TastyTrade')
    front = next((f for f in active if f.active_month), None)
    if front is None:
        front = next((f for f in active if f.next_active_month), None)
    if front is None:
        active.sort(key=lambda f: f.expiration_date)
        front = active[0]
    streamer = (front.streamer_symbol or '').strip() or front.symbol
    return FutureContract(
        product_code=product_code,
        symbol=front.symbol,
        streamer_symbol=streamer,
        expiration_date=str(front.expiration_date),
    )


def quote_from_market_data(md, contract: FutureContract) -> Optional[LiveQuote]:
    bid = _f(getattr(md, 'bid', None))
    ask = _f(getattr(md, 'ask', None))
    last = _f(getattr(md, 'last', None))
    mid = _f(getattr(md, 'mid', None)) or _mid(bid, ask)
    if bid is None and ask is None and last is None and mid is None:
        return None
    return LiveQuote(
        symbol=contract.symbol,
        streamer_symbol=contract.streamer_symbol,
        bid=bid,
        ask=ask,
        last=last,
        mid=mid,
        ts=time.time(),
        source='rest',
    )


async def fetch_rest_quote(session, contract: FutureContract) -> Optional[LiveQuote]:
    """REST snapshot via /market-data/by-type?future=/MNQU6 (single-symbol GET is empty)."""
    from tastytrade.market_data import get_market_data_by_type

    items = await get_market_data_by_type(session, futures=[contract.symbol])
    if not items:
        log.warning('REST by-type empty for %s', contract.symbol)
        return None
    quote = quote_from_market_data(items[0], contract)
    if quote is None:
        log.warning('REST by-type returned no bid/ask/last for %s', contract.symbol)
        return None
    log.info(
        'REST %s last=%s bid=%s ask=%s mid=%s',
        contract.symbol,
        quote.last,
        quote.bid,
        quote.ask,
        quote.mid,
    )
    return quote
