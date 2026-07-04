"""TastyTrade broker implementation using tastytrade SDK v12+.

Order price sign (SDK v12+): negative = debit, positive = credit.
All single-leg and stop-limit orders route through _signed_order_price().
Spread opens use positive net credit. Schwab legacy path is separate (meic0dte/order/).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional

from brokers.base import BrokerBase, OrderResult
from common import tt_config
from common.mqtt_prices import ensure_cache_started
from common.option_ticks import round_spx_option_price
from common.symbols import symbols_equivalent, to_schwab, to_tastytrade

log = logging.getLogger(__name__)

_NON_RETRYABLE = ('margin', 'insufficient', 'invalid', 'rejected', 'not found')


def _retry_on_transient(func, max_retries=3, base_delay=2.0):
    """Retry on transient errors (network, 500s). Not on business errors (margin, invalid order)."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            err_str = str(e).lower()
            if any(term in err_str for term in _NON_RETRYABLE):
                raise
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                log.warning(
                    "Broker call failed (attempt %d/%d), retrying in %.1fs: %s",
                    attempt + 1, max_retries, delay, e,
                )
                time.sleep(delay)
    raise last_exc


def _round_option_price(val: float) -> Decimal:
    return Decimal(str(round_spx_option_price(abs(val))))


def _normalize_leg_action(action: Any) -> str:
    """Map SDK leg actions ('Sell to Open') to SELL_TO_OPEN for parsing."""
    text = str(action or '').upper().replace(' ', '_').replace('-', '_')
    while '__' in text:
        text = text.replace('__', '_')
    return text


def _signed_order_price(action: str, amount: float) -> Decimal:
    """
    TastyTrade SDK price sign: negative = debit, positive = credit.

    BUY_TO_* closes/opens for debit; SELL_TO_* for credit.
    """
    magnitude = _round_option_price(abs(amount))
    if action in ('BUY_TO_OPEN', 'BUY_TO_CLOSE'):
        return -magnitude
    if action in ('SELL_TO_OPEN', 'SELL_TO_CLOSE'):
        return magnitude
    raise ValueError(f'Unsupported order action: {action}')


def _order_result_from_response(resp) -> OrderResult:
    """Parse tastytrade PlacedOrderResponse (SDK v12+)."""
    errors = getattr(resp, 'errors', None) or []
    if errors:
        msg = '; '.join(getattr(e, 'message', str(e)) for e in errors)
        return OrderResult(False, None, 'rejected', message=msg or 'Order rejected')

    order = resp.order
    if order is None:
        return OrderResult(True, None, 'working')
    return _order_result_from_placed_order(order)


def _leg_filled_qty(leg) -> int:
    if getattr(leg, 'fills', None):
        return sum(int(f.quantity) for f in leg.fills)
    qty = getattr(leg, 'quantity', None)
    rem = getattr(leg, 'remaining_quantity', None)
    if qty is not None and rem is not None:
        return max(0, int(qty) - int(rem))
    return 0


def _leg_avg_fill_price(leg) -> Optional[float]:
    """Average fill price magnitude for a single leg (from TT fill records)."""
    fills = getattr(leg, 'fills', None) or []
    if not fills:
        return None
    total_qty = 0
    weighted = 0.0
    for f in fills:
        q = int(f.quantity)
        if q <= 0:
            continue
        total_qty += q
        weighted += abs(float(f.fill_price)) * q
    if total_qty <= 0:
        return round(abs(float(fills[-1].fill_price)), 2)
    return round(weighted / total_qty, 2)


def _spread_filled_quantity(short_filled: int, long_filled: int) -> int:
    """
    Vertical spread: a partial fill is N spread units — both legs fill N together.
    Do not count contracts until both legs report fills.
    """
    if short_filled <= 0 or long_filled <= 0:
        return 0
    return min(short_filled, long_filled)


def _order_result_from_placed_order(order) -> OrderResult:
    """Build OrderResult from a tastytrade PlacedOrder (live or response)."""
    order_id = None
    if getattr(order, 'id', None) not in (None, -1):
        order_id = str(order.id)

    status = str(getattr(order, 'status', 'working')).lower()
    order_qty = int(order.size) if getattr(order, 'size', None) is not None else 0
    legs = getattr(order, 'legs', None) or []

    short_filled = long_filled = 0
    short_fill = long_fill = None
    buy_to_close_filled = sell_to_close_filled = 0
    buy_to_close_fill = sell_to_close_fill = None
    for leg in legs:
        fq = _leg_filled_qty(leg)
        action = _normalize_leg_action(getattr(leg, 'action', ''))
        leg_px = _leg_avg_fill_price(leg)
        if action == 'SELL_TO_OPEN':
            short_filled = fq
            if leg_px is not None:
                short_fill = leg_px
        elif action == 'BUY_TO_OPEN':
            long_filled = fq
            if leg_px is not None:
                long_fill = leg_px
        elif action == 'BUY_TO_CLOSE':
            buy_to_close_filled = fq
            if leg_px is not None:
                buy_to_close_fill = leg_px
        elif action == 'SELL_TO_CLOSE':
            sell_to_close_filled = fq
            if leg_px is not None:
                sell_to_close_fill = leg_px

    if not order_qty and legs:
        order_qty = max(int(getattr(leg, 'quantity', 0) or 0) for leg in legs)

    filled_qty = _spread_filled_quantity(short_filled, long_filled)
    single_leg_fill = None
    if buy_to_close_fill is not None and sell_to_close_fill is None and short_fill is None:
        single_leg_fill = buy_to_close_fill
        filled_qty = buy_to_close_filled or filled_qty
    elif sell_to_close_fill is not None and buy_to_close_fill is None and long_fill is None:
        single_leg_fill = sell_to_close_fill
        filled_qty = sell_to_close_filled or filled_qty

    if status == 'filled' and order_qty and not single_leg_fill:
        filled_qty = order_qty
    elif status == 'filled' and order_qty and single_leg_fill:
        filled_qty = order_qty or filled_qty

    remaining = max(0, order_qty - filled_qty) if order_qty else None
    spread_credit = None
    if short_fill is not None and long_fill is not None:
        spread_credit = round(short_fill - long_fill, 2)
    elif single_leg_fill is not None:
        spread_credit = single_leg_fill
    elif order.price is not None and filled_qty > 0:
        spread_credit = round(abs(float(order.price)), 2)

    if filled_qty > 0 and status == 'working':
        status = 'partial'

    return OrderResult(
        success=True,
        order_id=order_id,
        status=status,
        filled_price=spread_credit,
        filled_quantity=filled_qty,
        order_quantity=order_qty or None,
        remaining_quantity=remaining,
        short_fill_price=short_fill,
        long_fill_price=long_fill,
        raw=order,
    )


class TastyTradeBroker(BrokerBase):
    """Sync wrapper around async tastytrade SDK (dedicated background event loop)."""

    def __init__(self, session: Any, account: Any = None):
        self.session = session
        self._is_paper = hasattr(session, 'api_key')
        self._option_cache: Dict[str, Any] = {}
        self._loop = asyncio.new_event_loop()
        self._loop_ready = threading.Event()
        self._loop_thread = threading.Thread(
            target=self._loop_worker,
            name='tt-broker-loop',
            daemon=True,
        )
        self._loop_thread.start()
        self._loop_ready.wait(timeout=30)
        if account is None:
            self.account = self._run(self._bootstrap_account())
        else:
            self.account = account
        self._connected = True
        self._prices = ensure_cache_started()

    def _loop_worker(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def _run(self, coro, timeout: float = 120):
        """Thread-safe: monitors call broker from parallel threads."""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _bootstrap_account(self) -> Any:
        from tastytrade import Account

        acct_num = tt_config.TT_ACCOUNT_NUMBER
        if not acct_num:
            raise ValueError('TT_ACCOUNT_NUMBER must be set in .env')

        await self.session.validate()
        if self._is_paper:
            accounts = await Account.a_get(self.session)
            for a in accounts:
                if str(a.account_number) == str(acct_num):
                    return a
            if accounts:
                return accounts[0]
            raise ValueError('No paper accounts found')
        return await Account.get(self.session, acct_num)

    def connect(self) -> bool:
        try:
            self._run(self.session.validate())
            self._connected = True
            self.start_session_refresh()
            return True
        except Exception as exc:
            log.error('TastyTrade connect failed: %s', exc)
            return False

    def validate_session(self) -> None:
        """Validate the current session; re-create it if validation fails."""
        try:
            self._run(self.session.validate())
            log.debug('Session validation OK')
        except Exception as exc:
            log.warning('Session validation failed (%s), re-creating session ...', exc)
            try:
                from common.tt_auth import create_tastytrade_session
                self.session = create_tastytrade_session()
                self._run(self.session.validate())
                log.info('Session re-created and validated successfully')
            except Exception as exc2:
                log.error('Session re-creation failed: %s', exc2)

    def start_session_refresh(self, interval_sec: int = 1200) -> None:
        """Start a daemon thread that re-validates the session every *interval_sec* seconds."""
        def _refresh_loop():
            while True:
                time.sleep(interval_sec)
                self.validate_session()

        t = threading.Thread(target=_refresh_loop, name='tt-session-refresh', daemon=True)
        t.start()
        log.info('Session refresh thread started (every %ds)', interval_sec)

    async def _get_option(self, streamer_symbol: str) -> Any:
        """Return a cached Option object, fetching the chain only on cache miss."""
        cached = self._option_cache.get(streamer_symbol)
        if cached is not None:
            return cached

        from tastytrade.instruments import get_option_chain
        chain = await get_option_chain(self.session, 'SPX')
        for _exp, options in chain.items():
            for opt in options:
                ss = getattr(opt, 'streamer_symbol', '') or ''
                occ = getattr(opt, 'symbol', '') or ''
                if ss:
                    self._option_cache[ss] = opt
                if occ:
                    self._option_cache[occ] = opt
        result = self._option_cache.get(streamer_symbol)
        if result is None:
            raise ValueError(f'Option not found in chain: {streamer_symbol}')
        return result

    def place_spread_order(
        self, short_symbol: str, long_symbol: str, qty: int, credit: float
    ) -> OrderResult:
        from tastytrade.order import (
            NewOrder,
            OrderAction,
            OrderTimeInForce,
            OrderType,
        )

        short_tt = to_tastytrade(short_symbol)
        long_tt = to_tastytrade(long_symbol)

        async def _build_and_place():
            short_opt = await self._get_option(short_tt)
            long_opt = await self._get_option(long_tt)

            legs = [
                short_opt.build_leg(Decimal(qty), OrderAction.SELL_TO_OPEN),
                long_opt.build_leg(Decimal(qty), OrderAction.BUY_TO_OPEN),
            ]
            order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=legs,
                price=_round_option_price(credit),
            )
            return await self.account.place_order(self.session, order, dry_run=False)

        try:
            resp = _retry_on_transient(lambda: self._run(_build_and_place()))
            result = _order_result_from_response(resp)
            if not result.order_id:
                return OrderResult(
                    False, None, 'rejected',
                    message='No order id returned (was dry_run enabled?)',
                    raw=resp,
                )
            return result
        except Exception as exc:
            if '401' in str(exc) or 'unauthorized' in str(exc).lower():
                self.validate_session()
            return OrderResult(False, None, 'rejected', message=str(exc))

    def place_spread_close_order(
        self, short_symbol: str, long_symbol: str, qty: int, debit_limit: float
    ) -> OrderResult:
        from tastytrade.order import (
            NewOrder,
            OrderAction,
            OrderTimeInForce,
            OrderType,
        )

        short_tt = to_tastytrade(short_symbol)
        long_tt = to_tastytrade(long_symbol)

        async def _build_and_place():
            short_opt = await self._get_option(short_tt)
            long_opt = await self._get_option(long_tt)

            legs = [
                short_opt.build_leg(Decimal(qty), OrderAction.BUY_TO_CLOSE),
                long_opt.build_leg(Decimal(qty), OrderAction.SELL_TO_CLOSE),
            ]
            # TastyTrade: negative price = debit, positive = credit (see NewOrder.price docstring).
            order = NewOrder(
                time_in_force=OrderTimeInForce.DAY,
                order_type=OrderType.LIMIT,
                legs=legs,
                price=-_round_option_price(debit_limit),
            )
            return await self.account.place_order(self.session, order, dry_run=False)

        try:
            resp = _retry_on_transient(lambda: self._run(_build_and_place()))
            result = _order_result_from_response(resp)
            if not result.order_id:
                return OrderResult(
                    False, None, 'rejected',
                    message='No order id returned (spread close)',
                    raw=resp,
                )
            return result
        except Exception as exc:
            if '401' in str(exc) or 'unauthorized' in str(exc).lower():
                self.validate_session()
            return OrderResult(False, None, 'rejected', message=str(exc))

    def place_stop_order(
        self, symbol: str, qty: int, stop_price: float, limit_price: float
    ) -> OrderResult:
        return self._place_single_leg_order(
            'BUY_TO_CLOSE', symbol, qty,
            order_kind='STOP_LIMIT',
            stop_price=stop_price, limit_price=limit_price,
        )

    def place_limit_order(
        self, action: str, symbol: str, qty: int, price: float
    ) -> OrderResult:
        return self._place_single_leg_order(action, symbol, qty, limit_price=price)

    def place_market_order(self, action: str, symbol: str, qty: int) -> OrderResult:
        return self._place_single_leg_order(action, symbol, qty, market=True)

    def _place_single_leg_order(
        self,
        action: str,
        symbol: str,
        qty: int,
        order_kind: str = 'LIMIT',
        stop_price: Optional[float] = None,
        limit_price: Optional[float] = None,
        market: bool = False,
    ) -> OrderResult:
        from tastytrade.order import (
            NewOrder,
            OrderAction,
            OrderTimeInForce,
            OrderType as OT,
        )

        tt_sym = to_tastytrade(symbol)
        action_enum = getattr(OrderAction, action)

        async def _place():
            opt = await self._get_option(tt_sym)

            leg = opt.build_leg(Decimal(qty), action_enum)
            if market:
                order = NewOrder(
                    time_in_force=OrderTimeInForce.DAY,
                    order_type=OT.MARKET,
                    legs=[leg],
                )
            elif order_kind == 'STOP_LIMIT':
                order = NewOrder(
                    time_in_force=OrderTimeInForce.DAY,
                    order_type=OT.STOP_LIMIT,
                    legs=[leg],
                    stop_trigger=_round_option_price(abs(stop_price)),
                    price=_signed_order_price(action, limit_price),
                )
            else:
                order = NewOrder(
                    time_in_force=OrderTimeInForce.DAY,
                    order_type=OT.LIMIT,
                    legs=[leg],
                    price=_signed_order_price(action, limit_price),
                )
            return await self.account.place_order(self.session, order, dry_run=False)

        try:
            resp = _retry_on_transient(lambda: self._run(_place()))
            result = _order_result_from_response(resp)
            if not result.order_id:
                return OrderResult(
                    False, None, 'rejected',
                    message='No order id returned (was dry_run enabled?)',
                    raw=resp,
                )
            return result
        except Exception as exc:
            if '401' in str(exc) or 'unauthorized' in str(exc).lower():
                self.validate_session()
            return OrderResult(False, None, 'rejected', message=str(exc))

    def replace_order(self, order_id: str, new_spec: Dict) -> OrderResult:
        order_type = new_spec.get('orderType', 'LIMIT')
        symbol = new_spec.get('symbol', '')
        qty = int(new_spec.get('quantity', 1))
        if order_type == 'STOP_LIMIT':
            return self._replace_stop(order_id, symbol, qty, new_spec)
        if order_type == 'MARKET':
            self.cancel_order(order_id)
            return self.place_market_order('BUY_TO_CLOSE', symbol, qty)
        price = float(new_spec.get('price', 0))
        return self._replace_limit(order_id, symbol, qty, price)

    def _replace_stop(self, order_id: str, symbol: str, qty: int, spec: Dict) -> OrderResult:
        self.cancel_order(order_id)
        return self.place_stop_order(
            symbol, qty, float(spec['stopPrice']), float(spec['price'])
        )

    def _replace_limit(self, order_id: str, symbol: str, qty: int, price: float) -> OrderResult:
        self.cancel_order(order_id)
        return self.place_limit_order('BUY_TO_CLOSE', symbol, qty, price)

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            _retry_on_transient(
                lambda: self._run(self.account.delete_order(self.session, int(order_id)))
            )
            return OrderResult(True, order_id, 'cancelled', message='Cancelled')
        except Exception as exc:
            msg = str(exc)
            if 'filled' in msg.lower():
                return OrderResult(True, order_id, 'filled', message=msg)
            if '401' in msg or 'unauthorized' in msg.lower():
                self.validate_session()
            return OrderResult(False, order_id, 'rejected', message=msg)

    def get_order_status(self, order_id: str) -> OrderResult:
        try:
            orders = self._run(self.account.get_live_orders(self.session))
            for o in orders:
                if str(o.id) == str(order_id):
                    return _order_result_from_placed_order(o)
            # Filled/cancelled stops leave the live book — fetch by id.
            order = self._run(self.account.get_order(self.session, int(order_id)))
            return _order_result_from_placed_order(order)
        except Exception as exc:
            return OrderResult(False, order_id, 'rejected', message=str(exc))

    _ACTIVE_ORDER_STATUSES = frozenset(
        {'live', 'working', 'received', 'contingent', 'open', 'partially filled'}
    )

    def find_working_close_orders(self, symbol: str) -> List[OrderResult]:
        """Live BUY_TO_CLOSE orders on the short leg (stop, limit, or market)."""
        try:
            orders = self._run(self.account.get_live_orders(self.session))
        except Exception as exc:
            log.warning('find_working_close_orders failed: %s', exc)
            return []

        matches: List[OrderResult] = []
        for order in orders:
            status = str(getattr(order, 'status', '')).lower()
            if status not in self._ACTIVE_ORDER_STATUSES:
                continue
            for leg in getattr(order, 'legs', None) or []:
                leg_sym = str(getattr(leg, 'symbol', '') or '')
                action = _normalize_leg_action(getattr(leg, 'action', ''))
                if action != 'BUY_TO_CLOSE':
                    continue
                if symbols_equivalent(leg_sym, symbol):
                    matches.append(_order_result_from_placed_order(order))
                    break
        return matches

    def find_working_close_order(self, symbol: str) -> Optional[OrderResult]:
        orders = self.find_working_close_orders(symbol)
        return orders[0] if orders else None

    @staticmethod
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

    def fetch_spx_price_api(self) -> Optional[float]:
        from tastytrade.market_data import get_market_data
        from tastytrade.order import InstrumentType

        try:
            md = _retry_on_transient(
                lambda: self._run(get_market_data(self.session, 'SPX', InstrumentType.INDEX))
            )
            return self._mid_from_market_data(md)
        except Exception as exc:
            log.warning('fetch_spx_price_api failed: %s', exc)
            return None

    def fetch_option_mids_api(self, symbols: List[str]) -> Dict[str, float]:
        from tastytrade.market_data import get_market_data_by_type

        # REST /market-data/by-type expects Schwab/OCC symbols, not DXLink streamer symbols.
        tt_keys = list(dict.fromkeys(to_tastytrade(s) for s in symbols if s))
        api_symbols = list(dict.fromkeys(to_schwab(s) for s in tt_keys))
        out: Dict[str, float] = {}
        for i in range(0, len(api_symbols), 100):
            batch = api_symbols[i:i + 100]
            try:
                items = _retry_on_transient(
                    lambda b=batch: self._run(get_market_data_by_type(self.session, options=b))
                )
            except Exception as exc:
                log.warning('fetch_option_mids_api batch failed: %s', exc)
                time.sleep(1.0)
                continue
            for md in items:
                mid = self._mid_from_market_data(md)
                if mid is None:
                    continue
                sym = to_tastytrade(getattr(md, 'symbol', '') or '')
                if sym:
                    out[sym] = mid
            if i + 100 < len(api_symbols):
                time.sleep(0.25)
        return out

    def get_option_prices(self, symbols: List[str]) -> Dict[str, float]:
        out: Dict[str, float] = {}
        for s in symbols:
            price = self._prices.get(s)
            if price is None:
                continue
            key = 'SPX' if s.upper() == 'SPX' else to_tastytrade(s)
            out[key] = price
        return out

    def get_option_price(self, symbol: str, timeout: float = 0) -> Optional[float]:
        if timeout > 0:
            return self._prices.wait_for(symbol, timeout=timeout)
        return self._prices.get(symbol)

    def get_spx_price(self) -> Optional[float]:
        price = self._prices.get_spx()
        if price is None:
            price = self._prices.wait_for('SPX', timeout=15.0)
        if price is None:
            log.warning(
                'SPX not available on MQTT — is streaming/publish_tastytrade.py running?'
            )
        return price
