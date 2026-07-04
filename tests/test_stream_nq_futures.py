#!/usr/bin/env python3
"""
Explore streaming NQ / MNQ futures via TastyTrade DXLink (tastyware).

Mirrors the SPX pattern in streaming/publish_tastytrade.py:
  1. Resolve front-month contract via instruments API
  2. Optional REST snapshot (get_market_data)
  3. Subscribe Quote + Trade on DXLinkStreamer and log ticks

Run (paper/tastyware):
    python tests/test_stream_nq_futures.py --symbol MNQ --seconds 30 --paper

Run (live OAuth):
    python tests/test_stream_nq_futures.py --symbol NQ --seconds 30

Also compare against SPX on the same stream:
    python tests/test_stream_nq_futures.py --symbol MNQ --with-spx --seconds 60 --paper -v
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

log = logging.getLogger('nq_stream')


def _setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format='%(asctime)s [NQ-STREAM] %(levelname)s %(message)s',
    )


@dataclass
class FutureContract:
    product_code: str
    symbol: str
    streamer_symbol: str
    expiration_date: str
    active_month: bool
    next_active_month: bool


def _mid_from_market_data(md) -> Optional[float]:
    for attr in ('mid', 'mark', 'last'):
        val = getattr(md, attr, None)
        if val is not None:
            f = float(val)
            if f > 0:
                return f
    bid = getattr(md, 'bid', None)
    ask = getattr(md, 'ask', None)
    if bid is not None and ask is not None:
        b, a = float(bid), float(ask)
        if b > 0 and a > 0:
            return (b + a) / 2.0
    return None


async def _resolve_front_future(session, product_code: str) -> FutureContract:
    from tastytrade.instruments import Future

    code = product_code.upper().replace('/', '')
    futures = await Future.get(session, product_codes=[code])
    if not isinstance(futures, list):
        futures = [futures]

    active = [f for f in futures if getattr(f, 'active', False)]
    if not active:
        raise ValueError(f'No active {code} futures returned from TastyTrade')

    front = next((f for f in active if f.active_month), None)
    if front is None:
        front = next((f for f in active if f.next_active_month), None)
    if front is None:
        active.sort(key=lambda f: f.expiration_date)
        front = active[0]

    streamer = (front.streamer_symbol or '').strip()
    if not streamer:
        sym = front.symbol.replace('/', '')
        streamer = f'/{sym}' if not sym.startswith('/') else sym

    return FutureContract(
        product_code=code,
        symbol=front.symbol,
        streamer_symbol=streamer,
        expiration_date=str(front.expiration_date),
        active_month=bool(front.active_month),
        next_active_month=bool(front.next_active_month),
    )


def _stream_subscribe_symbols(contract: FutureContract) -> list[str]:
    """Candidate DXLink symbols to try (streamer_symbol is preferred)."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in (
        contract.streamer_symbol,
        contract.symbol,
        contract.symbol.lstrip('/'),
        f'/{contract.symbol.lstrip("/")}',
        contract.product_code,
    ):
        s = (raw or '').strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


async def _fetch_rest_snapshot(session, contract: FutureContract) -> Optional[float]:
    from tastytrade.market_data import get_market_data
    from tastytrade.order import InstrumentType

    sym = contract.symbol.lstrip('/')
    try:
        md = await get_market_data(session, sym, InstrumentType.FUTURE)
    except Exception as exc:
        log.warning('REST get_market_data(%s) failed: %s', sym, exc)
        return None
    mid = _mid_from_market_data(md)
    log.info(
        'REST snapshot %s: mid=%s bid=%s ask=%s last=%s',
        sym,
        mid,
        getattr(md, 'bid', None),
        getattr(md, 'ask', None),
        getattr(md, 'last', None),
    )
    return mid


def _quote_mid(quote) -> Optional[float]:
    bid = quote.bid_price
    ask = quote.ask_price
    if bid is None and ask is None:
        return None
    b = float(bid or 0)
    a = float(ask or 0)
    if b <= 0 and a <= 0:
        return None
    return (b + a) / 2 if b and a else (b or a)


def _normalize_label(event_symbol: str, contract: FutureContract) -> str:
    sym = (event_symbol or '').strip()
    aliases = {
        contract.streamer_symbol,
        contract.symbol,
        contract.symbol.lstrip('/'),
        f'/{contract.symbol.lstrip("/")}',
        contract.product_code,
        'SPX', '$SPX', '.$SPX',
    }
    if sym in aliases or sym.upper() == contract.product_code:
        return contract.product_code
    if sym in ('SPX', '$SPX', '.$SPX'):
        return 'SPX'
    return sym


async def _stream_prices(
    session,
    contract: FutureContract,
    *,
    seconds: float,
    with_spx: bool,
    subscribe_symbols: list[str],
) -> dict[str, float]:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Trade

    last_prices: dict[str, float] = {}
    quote_count = 0
    trade_count = 0
    deadline = time.monotonic() + seconds

    trade_syms = list(subscribe_symbols)
    if with_spx:
        trade_syms = list(dict.fromkeys(trade_syms + ['SPX', '$SPX']))

    log.info(
        'DXLink subscribe Quote=%s Trade=%s for %ss',
        subscribe_symbols,
        trade_syms,
        seconds,
    )

    async with DXLinkStreamer(session) as streamer:
        await streamer.subscribe(Quote, subscribe_symbols)
        await streamer.subscribe(Trade, trade_syms)

        async def _quotes():
            nonlocal quote_count
            async for quote in streamer.listen(Quote):
                mid = _quote_mid(quote)
                if mid is None:
                    continue
                label = _normalize_label(quote.event_symbol, contract)
                last_prices[label] = mid
                quote_count += 1
                log.info(
                    'QUOTE %s bid=%s ask=%s mid=%.4f (raw=%s)',
                    label,
                    quote.bid_price,
                    quote.ask_price,
                    mid,
                    quote.event_symbol,
                )

        async def _trades():
            nonlocal trade_count
            async for trade in streamer.listen(Trade):
                price = float(trade.price or 0)
                if price <= 0:
                    continue
                label = _normalize_label(trade.event_symbol, contract)
                last_prices[label] = price
                trade_count += 1
                log.info(
                    'TRADE %s price=%.4f size=%s (raw=%s)',
                    label,
                    price,
                    trade.day_volume,
                    trade.event_symbol,
                )

        quote_task = asyncio.create_task(_quotes())
        trade_task = asyncio.create_task(_trades())

        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(0.5)
        finally:
            quote_task.cancel()
            trade_task.cancel()
            for task in (quote_task, trade_task):
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    log.info(
        'Stream finished: quotes=%d trades=%d last_prices=%s',
        quote_count,
        trade_count,
        last_prices,
    )
    return last_prices


async def _run(args) -> int:
    from common.tt_auth import create_tastytrade_session

    product = args.symbol.upper().replace('/', '')
    if product not in ('NQ', 'MNQ'):
        log.error('Unsupported symbol %s — use NQ or MNQ', args.symbol)
        return 1

    session = create_tastytrade_session(paper=args.paper)
    log.info(
        'Session created (paper=%s, tastyware=%s)',
        args.paper,
        bool(getattr(session, 'api_key', None)),
    )

    if hasattr(session, 'validate'):
        await session.validate()
        log.info('Session validated')

    contract = await _resolve_front_future(session, product)
    log.info(
        'Resolved %s: symbol=%s streamer=%s expiry=%s active_month=%s next=%s',
        product,
        contract.symbol,
        contract.streamer_symbol,
        contract.expiration_date,
        contract.active_month,
        contract.next_active_month,
    )

    rest_mid = await _fetch_rest_snapshot(session, contract)
    if rest_mid is None:
        log.warning('No REST mid — will rely on stream only')

    subscribe_symbols = _stream_subscribe_symbols(contract)
    if with_spx := args.with_spx:
        subscribe_symbols = list(dict.fromkeys(subscribe_symbols + ['SPX']))

    last = await _stream_prices(
        session,
        contract,
        seconds=args.seconds,
        with_spx=with_spx,
        subscribe_symbols=subscribe_symbols,
    )

    ok = product in last
    if with_spx and 'SPX' not in last:
        log.warning('SPX baseline missing — stream may be down or market closed')

    if ok:
        log.info('SUCCESS: streamed %s last=%.4f', product, last[product])
        return 0

    log.error(
        'FAIL: no DXLink ticks for %s in %ss (got keys=%s). '
        'Try during CME Globex hours; verify streamer_symbol=%s',
        product,
        args.seconds,
        list(last.keys()),
        contract.streamer_symbol,
    )
    if rest_mid is not None:
        log.info('REST mid was available (%s=%.4f) — REST works, stream symbols may need adjustment', product, rest_mid)
        return 2
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Stream NQ/MNQ futures via TastyTrade DXLink (tastyware test)',
    )
    parser.add_argument(
        '--symbol',
        default='MNQ',
        choices=['NQ', 'MNQ', 'nq', 'mnq'],
        help='Future product code (default: MNQ)',
    )
    parser.add_argument(
        '--seconds',
        type=float,
        default=30.0,
        help='How long to listen for stream ticks (default: 30)',
    )
    parser.add_argument(
        '--paper',
        action='store_true',
        help='Use tastyware PaperSession (TASTYWARE_API_KEY)',
    )
    parser.add_argument(
        '--with-spx',
        action='store_true',
        help='Also subscribe SPX as a streaming baseline',
    )
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.paper:
        os.environ['PAPER_MODE'] = 'true'
        import importlib
        import common.tt_config as tc
        importlib.reload(tc)

    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        log.info('Interrupted')
        return 130
    except Exception as exc:
        log.exception('Fatal: %s', exc)
        return 1


if __name__ == '__main__':
    sys.exit(main())
