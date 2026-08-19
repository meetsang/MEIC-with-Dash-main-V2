"""DXLink streamer for MNQ. Quotes/trades only — never submits orders."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from mnq_paper.engine import PaperEngine
from mnq_paper.models import LiveQuote
from mnq_paper.quotes import FutureContract

log = logging.getLogger('mnq_paper.streamer')


def _quote_mid(quote) -> Optional[float]:
    bid = getattr(quote, 'bid_price', None)
    ask = getattr(quote, 'ask_price', None)
    b = float(bid or 0)
    a = float(ask or 0)
    if b <= 0 and a <= 0:
        return None
    if b > 0 and a > 0:
        return (b + a) / 2.0
    return b or a


async def stream_mnq(
    session,
    contract: FutureContract,
    engine: PaperEngine,
    stop_event: asyncio.Event,
) -> None:
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Trade

    symbols = [contract.streamer_symbol]
    backoff = 2.0
    while not stop_event.is_set():
        try:
            log.info('DXLink subscribe Quote+Trade %s', symbols)
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Quote, symbols)
                await streamer.subscribe(Trade, symbols)
                backoff = 2.0

                async def _quotes() -> None:
                    async for quote in streamer.listen(Quote):
                        if stop_event.is_set():
                            return
                        bid = float(quote.bid_price or 0) or None
                        ask = float(quote.ask_price or 0) or None
                        mid = _quote_mid(quote)
                        live = LiveQuote(
                            symbol=contract.symbol,
                            streamer_symbol=contract.streamer_symbol,
                            bid=bid if bid and bid > 0 else None,
                            ask=ask if ask and ask > 0 else None,
                            last=None,
                            mid=mid,
                            ts=time.time(),
                            source='quote',
                        )
                        closed = engine.update_quote(live)
                        if closed is not None:
                            log.info(
                                'BRACKET HIT %s %s exit=%.2f pnl=$%.2f',
                                closed.reason,
                                closed.outcome,
                                closed.exit,
                                closed.pnl_usd,
                            )

                async def _trades() -> None:
                    async for trade in streamer.listen(Trade):
                        if stop_event.is_set():
                            return
                        price = float(trade.price or 0)
                        if price <= 0:
                            continue
                        prev = engine.quote
                        live = LiveQuote(
                            symbol=contract.symbol,
                            streamer_symbol=contract.streamer_symbol,
                            bid=prev.bid if prev else None,
                            ask=prev.ask if prev else None,
                            last=price,
                            mid=prev.mid if prev else price,
                            ts=time.time(),
                            source='trade',
                        )
                        closed = engine.update_quote(live)
                        if closed is not None:
                            log.info(
                                'BRACKET HIT %s %s exit=%.2f pnl=$%.2f',
                                closed.reason,
                                closed.outcome,
                                closed.exit,
                                closed.pnl_usd,
                            )

                quote_task = asyncio.create_task(_quotes())
                trade_task = asyncio.create_task(_trades())
                try:
                    await stop_event.wait()
                finally:
                    quote_task.cancel()
                    trade_task.cancel()
                    for task in (quote_task, trade_task):
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if stop_event.is_set():
                return
            log.warning('DXLink dropped (%s) — reconnect in %.0fs', exc, backoff)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)
