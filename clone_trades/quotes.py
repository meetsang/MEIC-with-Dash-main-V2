"""Market data quote fetcher for option contracts."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any, Dict, Optional

from clone_trades.models import OptionSpec

log = logging.getLogger("clone_trades.quotes")


@dataclass
class OptionQuote:
    streamer_symbol: str
    occ_symbol: str
    bid: Optional[float]
    ask: Optional[float]
    last: Optional[float]
    mid: Optional[float]
    updated_at: float

    @property
    def market_buy_price(self) -> float:
        """Best available price to buy to open."""
        if self.ask is not None and self.ask > 0:
            return self.ask
        if self.mid is not None and self.mid > 0:
            return self.mid
        if self.last is not None and self.last > 0:
            return self.last
        if self.bid is not None and self.bid > 0:
            return self.bid
        return 0.0

    @property
    def market_sell_price(self) -> float:
        """Best available price to sell to close."""
        if self.bid is not None and self.bid > 0:
            return self.bid
        if self.mid is not None and self.mid > 0:
            return self.mid
        if self.last is not None and self.last > 0:
            return self.last
        if self.ask is not None and self.ask > 0:
            return self.ask
        return 0.0


class OptionQuoteService:
    """Fetches and caches option quotes from TastyTrade REST or fallback."""

    def __init__(self, broker=None):
        self.broker = broker
        self._cache: Dict[str, OptionQuote] = {}
        self._cache_ttl_sec: float = 2.0
        self._mock_prices: Dict[str, Dict[str, float]] = {}

    def set_mock_price(self, symbol: str, price: float, ask: Optional[float] = None, bid: Optional[float] = None) -> None:
        """Helper for unit testing and simulated feeds."""
        self._mock_prices[symbol] = {
            "price": float(price),
            "ask": float(ask) if ask is not None else float(price),
            "bid": float(bid) if bid is not None else float(price),
        }

    def clear_mock_price(self, symbol: str) -> None:
        self._mock_prices.pop(symbol, None)

    def get_quote(self, spec: OptionSpec) -> OptionQuote:
        now = time.time()
        sym = spec.streamer_symbol
        occ = spec.occ_symbol

        # Check mock prices first (useful for tests or manual testing)
        if sym in self._mock_prices or occ in self._mock_prices:
            mock_data = self._mock_prices.get(sym) or self._mock_prices.get(occ) or {}
            px = mock_data.get("price", 1.0)
            ask = mock_data.get("ask", px)
            bid = mock_data.get("bid", px)
            return OptionQuote(
                streamer_symbol=sym,
                occ_symbol=occ,
                bid=bid,
                ask=ask,
                last=px,
                mid=round((bid + ask) / 2.0, 2),
                updated_at=now,
            )

        # Check in-memory cache
        cached = self._cache.get(sym)
        if cached and (now - cached.updated_at) < self._cache_ttl_sec:
            return cached

        # Attempt to fetch quote via broker
        quote = self._fetch_from_broker(spec)
        if quote:
            self._cache[sym] = quote
            return quote

        # If cached quote exists even if slightly stale, return it
        if cached:
            return cached

        # Default fallback quote
        fallback = OptionQuote(
            streamer_symbol=sym,
            occ_symbol=occ,
            bid=None,
            ask=None,
            last=None,
            mid=None,
            updated_at=now,
        )
        return fallback

    def _fetch_from_broker(self, spec: OptionSpec) -> Optional[OptionQuote]:
        if not self.broker:
            # Try to get shared broker
            try:
                from common.broker_factory import get_shared_broker
                self.broker = get_shared_broker()
            except Exception:
                pass

        if not self.broker or not hasattr(self.broker, "session"):
            return None

        session = getattr(self.broker, "session", None)
        if not session:
            return None

        try:
            from tastytrade.market_data import get_market_data_by_type

            # TastyTrade REST /market-data/by-type requires the OCC symbol format (e.g. 'SPY   260921C00765000')
            coro = get_market_data_by_type(session, options=[spec.occ_symbol])
            if hasattr(self.broker, "_run"):
                results = self.broker._run(coro)
            else:
                try:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            results = pool.submit(lambda: asyncio.run(coro)).result(timeout=4)
                    else:
                        results = loop.run_until_complete(coro)
                except Exception:
                    results = asyncio.run(coro)

            if not results:
                return None

            md = results[0]
            bid = float(md.bid) if getattr(md, "bid", None) is not None else None
            ask = float(md.ask) if getattr(md, "ask", None) is not None else None
            last = float(md.last) if getattr(md, "last", None) is not None else None
            mid = float(md.mid) if getattr(md, "mid", None) is not None else None
            if mid is None and bid is not None and ask is not None:
                mid = (bid + ask) / 2.0

            return OptionQuote(
                streamer_symbol=spec.streamer_symbol,
                occ_symbol=spec.occ_symbol,
                bid=bid,
                ask=ask,
                last=last,
                mid=mid,
                updated_at=time.time(),
            )
        except Exception as exc:
            log.debug("REST quote fetch failed for %s: %s", spec.streamer_symbol, exc)
            return None
