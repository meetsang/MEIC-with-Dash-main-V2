"""Local paper engine. Never talks to the broker — prices are injected."""
from __future__ import annotations

import threading
import uuid
from typing import Optional

from mnq_paper.config import Settings
from mnq_paper.journal import Journal
from mnq_paper.models import (
    ClosedTrade,
    LiveQuote,
    OpenTrade,
    Side,
    round_tick,
    utc_now_iso,
)


class EngineError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def parse_side(raw: str) -> Side:
    text = (raw or '').strip().lower()
    if text in ('buy', 'long', 'bull', 'b'):
        return 'long'
    if text in ('sell', 'short', 'bear', 's'):
        return 'short'
    raise EngineError(f'Unknown side {raw!r} — use buy/sell or long/short')


def bracket_levels(side: Side, entry: float, stop_points: float, target_points: float, tick: float) -> tuple[float, float]:
    entry = round_tick(entry, tick)
    stop_pts = abs(float(stop_points))
    target_pts = abs(float(target_points))
    if stop_pts <= 0 or target_pts <= 0:
        raise EngineError('stop_points and target_points must be > 0')
    if side == 'long':
        stop = round_tick(entry - stop_pts, tick)
        target = round_tick(entry + target_pts, tick)
    else:
        stop = round_tick(entry + stop_pts, tick)
        target = round_tick(entry - target_pts, tick)
    return stop, target


def pnl_points(side: Side, entry: float, exit_px: float) -> float:
    if side == 'long':
        return round(exit_px - entry, 4)
    return round(entry - exit_px, 4)


def hit_reason(trade: OpenTrade, price: float) -> Optional[str]:
    """Return 'stop' or 'target' if the bracket is hit. Stop wins on a gap through both."""
    if trade.side == 'long':
        stop_hit = price <= trade.stop
        target_hit = price >= trade.target
    else:
        stop_hit = price >= trade.stop
        target_hit = price <= trade.target
    if stop_hit and target_hit:
        return 'stop'
    if stop_hit:
        return 'stop'
    if target_hit:
        return 'target'
    return None


class PaperEngine:
    def __init__(self, settings: Settings, journal: Journal) -> None:
        self.settings = settings
        self.journal = journal
        self._lock = threading.RLock()
        self.quote: Optional[LiveQuote] = None
        self.open: Optional[OpenTrade] = journal.load_open()
        self.last_closed: Optional[ClosedTrade] = None
        self.public_url: Optional[str] = None

    def set_public_url(self, url: Optional[str]) -> None:
        with self._lock:
            self.public_url = url

    def update_quote(self, quote: LiveQuote) -> Optional[ClosedTrade]:
        with self._lock:
            self.quote = quote
            mark = quote.mark()
            if mark is None or self.open is None:
                return None
            return self._maybe_close_locked(mark, quote.source)

    def open_signal(
        self,
        side_raw: str,
        *,
        fill: Optional[float] = None,
        qty: Optional[int] = None,
        stop_points: Optional[float] = None,
        target_points: Optional[float] = None,
        note: str = '',
        entry_source: str = 'rest',
    ) -> OpenTrade:
        side = parse_side(side_raw)
        with self._lock:
            if self.open is not None:
                raise EngineError(
                    f'Already in a paper {self.open.side} trade {self.open.id}',
                    status=409,
                )
            price = fill
            if price is None and self.quote is not None:
                price = self.quote.market_fill(side)
                entry_source = self.quote.source
            if price is None or price <= 0:
                raise EngineError('No MNQ price yet — wait for REST/streamer quote', status=503)
            qty_n = int(qty or self.settings.qty)
            if qty_n <= 0:
                raise EngineError('qty must be >= 1')
            stop_pts = float(self.settings.stop_points if stop_points is None else stop_points)
            target_pts = float(self.settings.target_points if target_points is None else target_points)
            entry = round_tick(float(price), self.settings.tick)
            stop, target = bracket_levels(side, entry, stop_pts, target_pts, self.settings.tick)
            trade = OpenTrade(
                id=uuid.uuid4().hex[:12],
                symbol=self.settings.rest_symbol,
                streamer_symbol=(self.quote.streamer_symbol if self.quote else self.settings.rest_symbol),
                side=side,
                qty=qty_n,
                entry=entry,
                stop=stop,
                target=target,
                stop_points=stop_pts,
                target_points=target_pts,
                opened_at=utc_now_iso(),
                entry_source=entry_source,
                note=note or '',
            )
            self.open = trade
            self.journal.save_open(trade)
            return trade

    def flatten(self, *, price: Optional[float] = None, source: str = 'flatten') -> ClosedTrade:
        with self._lock:
            if self.open is None:
                raise EngineError('No open paper trade to flatten', status=409)
            px = price
            if px is None and self.quote is not None:
                px = self.quote.mark()
            if px is None or px <= 0:
                raise EngineError('No price available to flatten', status=503)
            return self._close_locked(float(px), 'flatten', source)

    def status(self) -> dict:
        with self._lock:
            quote = None
            if self.quote is not None:
                quote = {
                    'symbol': self.quote.symbol,
                    'streamer_symbol': self.quote.streamer_symbol,
                    'bid': self.quote.bid,
                    'ask': self.quote.ask,
                    'last': self.quote.last,
                    'mid': self.quote.mid,
                    'source': self.quote.source,
                    'ts': self.quote.ts,
                }
            unrealized = None
            if self.open is not None and self.quote is not None:
                mark = self.quote.mark()
                if mark is not None:
                    pts = pnl_points(self.open.side, self.open.entry, mark)
                    unrealized = {
                        'points': pts,
                        'usd': round(pts * self.open.qty * self.settings.point_value, 2),
                    }
            return {
                'paper': True,
                'broker_orders': False,
                'product': self.settings.product_code,
                'symbol': self.settings.rest_symbol,
                'defaults': {
                    'qty': self.settings.qty,
                    'stop_points': self.settings.stop_points,
                    'target_points': self.settings.target_points,
                    'tick': self.settings.tick,
                    'point_value': self.settings.point_value,
                },
                'quote': quote,
                'open': None if self.open is None else self.open.to_dict(),
                'unrealized': unrealized,
                'last_closed': None if self.last_closed is None else self.last_closed.to_dict(),
                'public_url': self.public_url,
                'recent': self.journal.recent_closed(20),
            }

    def _maybe_close_locked(self, price: float, source: str) -> Optional[ClosedTrade]:
        trade = self.open
        if trade is None:
            return None
        reason = hit_reason(trade, price)
        if reason is None:
            return None
        return self._close_locked(price, reason, source)

    def _close_locked(self, price: float, reason: str, source: str) -> ClosedTrade:
        trade = self.open
        if trade is None:
            raise EngineError('No open trade')
        exit_px = round_tick(price, self.settings.tick)
        pts = pnl_points(trade.side, trade.entry, exit_px)
        if reason == 'target':
            outcome = 'win'
        elif reason == 'stop':
            outcome = 'loss'
        else:
            outcome = 'flattened'
        closed = ClosedTrade(
            id=trade.id,
            symbol=trade.symbol,
            streamer_symbol=trade.streamer_symbol,
            side=trade.side,
            qty=trade.qty,
            entry=trade.entry,
            stop=trade.stop,
            target=trade.target,
            exit=exit_px,
            outcome=outcome,
            reason=reason,  # type: ignore[arg-type]
            pnl_points=pts,
            pnl_usd=round(pts * trade.qty * self.settings.point_value, 2),
            opened_at=trade.opened_at,
            closed_at=utc_now_iso(),
            entry_source=trade.entry_source,
            exit_source=source,
            note=trade.note,
        )
        self.open = None
        self.last_closed = closed
        self.journal.save_open(None)
        self.journal.append_closed(closed)
        return closed
