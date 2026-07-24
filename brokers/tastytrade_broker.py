"""TastyTrade broker implementation using tastytrade SDK v12+.

Order price sign (SDK v12+): negative = debit, positive = credit.
All single-leg and stop-limit orders route through _signed_order_price().
Spread opens use positive net credit. Schwab legacy path is separate (meic0dte/order/).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from common import tt_config
from common.broker_cooldown import set_cooldown, should_skip_priority
from common.rest_limiter import get_rest_limiter
from common.mqtt_prices import ensure_cache_started
from common.option_ticks import round_spx_option_price
from common.symbols import symbols_equivalent, to_schwab, to_tastytrade
from brokers.base import BrokerBase, OrderResult

log = logging.getLogger(__name__)

_NON_RETRYABLE = ('margin', 'insufficient', 'invalid', 'rejected', 'not found')
_RETRYABLE_5XX = ('500', '502', '503', '504')


class BrokerCooldownActive(Exception):
    """REST call skipped — broker cooldown circuit breaker is active."""


class BrokerRateLimited(Exception):
    """REST call rejected by rate limiter or remote throttle."""


def _extract_retry_after_sec(exc: BaseException) -> Optional[float]:
    """Honor Retry-After when present on the exception or nested response."""
    for attr in ('retry_after', 'Retry-After', 'retry_after_sec'):
        val = getattr(exc, attr, None)
        if val is not None:
            try:
                return max(0.0, float(val))
            except (TypeError, ValueError):
                pass
    response = getattr(exc, 'response', None)
    if response is not None:
        headers = getattr(response, 'headers', None) or {}
        try:
            raw = headers.get('Retry-After') or headers.get('retry-after')
            if raw is not None:
                return max(0.0, float(raw))
        except (TypeError, ValueError, AttributeError):
            pass
    msg = str(exc)
    # e.g. "Retry-After: 5" in error text
    low = msg.lower()
    if 'retry-after' in low:
        try:
            after = low.split('retry-after', 1)[1]
            digits = ''.join(ch if (ch.isdigit() or ch == '.') else ' ' for ch in after)
            token = digits.strip().split()[0]
            return max(0.0, float(token))
        except (IndexError, ValueError):
            pass
    return None


def _is_non_retryable_broker_error(exc: BaseException) -> bool:
    if isinstance(exc, (BrokerRateLimited, BrokerCooldownActive)):
        return True
    err = str(exc).lower()
    if '429' in err or 'rate limit' in err:
        return True
    if any(tok in err for tok in ('401', '403', 'unauthorized', 'forbidden')):
        return True
    if any(term in err for term in _NON_RETRYABLE):
        return True
    # Explicit Retry-After means the server asked us to wait — do not hammer with 2s/4s
    if _extract_retry_after_sec(exc) is not None and ('429' in err or 'rate' in err):
        return True
    return False


def _is_retryable_network_or_5xx(exc: BaseException) -> bool:
    err = str(exc).lower()
    if any(tok in err for tok in (
        'timeout', 'timed out', 'connection', 'network', 'temporarily unavailable',
        'reset by peer', 'broken pipe',
    )):
        return True
    return any(code in err for code in _RETRYABLE_5XX)


def _signed_position_qty(pos: Any) -> int:
    """Normalize Tasty position qty to signed contracts (short < 0, long > 0).

    Tasty REST/WS reports positive ``quantity`` with ``quantity_direction``
    ('Short' / 'Long'). Fall back to raw signed qty when direction is absent.
    """
    raw = int(getattr(pos, 'quantity', 0) or 0)
    if raw == 0:
        return 0
    direction = getattr(pos, 'quantity_direction', None)
    if direction is None:
        direction = getattr(pos, 'quantity-direction', None)
    dir_s = str(direction or '').strip().lower()
    if dir_s == 'short':
        return -abs(raw)
    if dir_s == 'long':
        return abs(raw)
    return raw


def _retry_on_transient(func, max_retries=3, base_delay=2.0):
    """Bounded jittered retries for network / selected 5xx only.

    Non-retryable: HTTP 429, BrokerRateLimited, BrokerCooldownActive, 401, 403,
    and classic business rejects. When Retry-After is present on a 429-class
    error, honor it by sleeping once then raising (no 2s/4s retry loop).
    """
    import random

    last_exc = None
    for attempt in range(max_retries):
        try:
            return func()
        except Exception as e:
            if _is_non_retryable_broker_error(e):
                retry_after = _extract_retry_after_sec(e)
                if retry_after is not None and retry_after > 0:
                    log.warning(
                        'Broker non-retryable throttle — honoring Retry-After=%.1fs: %s',
                        retry_after,
                        e,
                    )
                    time.sleep(min(retry_after, 60.0))
                raise
            if not _is_retryable_network_or_5xx(e):
                raise
            last_exc = e
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                delay *= 0.5 + random.random()  # jitter in [0.5, 1.5) × base
                log.warning(
                    'Broker call failed (attempt %d/%d), retrying in %.1fs: %s',
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

    # Full spread close: BTC closes short, STC closes long.
    if buy_to_close_filled > 0 and sell_to_close_filled > 0:
        if buy_to_close_fill is not None:
            short_fill = buy_to_close_fill
        if sell_to_close_fill is not None:
            long_fill = sell_to_close_fill
        filled_qty = min(buy_to_close_filled, sell_to_close_filled)

    if status == 'filled' and order_qty and not single_leg_fill:
        filled_qty = order_qty
    elif status == 'filled' and order_qty and single_leg_fill:
        filled_qty = order_qty or filled_qty

    remaining = max(0, order_qty - filled_qty) if order_qty else None
    spread_credit = None
    filled_price_source = None
    order_limit_price = None
    broker_aggregate_fill_price = None

    if order.price is not None:
        order_limit_price = round(abs(float(order.price)), 2)

    if short_fill is not None and long_fill is not None:
        spread_credit = round(short_fill - long_fill, 2)
        filled_price_source = 'broker_leg_math'
    elif single_leg_fill is not None:
        spread_credit = single_leg_fill
        filled_price_source = 'broker_leg_fill'
    elif order_limit_price is not None and filled_qty > 0:
        spread_credit = order_limit_price
        filled_price_source = 'order_limit_fallback'

    if filled_qty > 0 and status == 'working':
        status = 'partial'

    filled_at = None
    try:
        latest = None
        for leg in legs:
            for fill in getattr(leg, 'fills', None) or []:
                ts = getattr(fill, 'filled_at', None) or getattr(fill, 'fill_time', None)
                if ts is None:
                    continue
                if hasattr(ts, 'timestamp'):
                    val = float(ts.timestamp())
                else:
                    val = float(datetime.fromisoformat(str(ts).replace('Z', '+00:00')).timestamp())
                if latest is None or val > latest:
                    latest = val
        filled_at = latest
    except (TypeError, ValueError, AttributeError):
        filled_at = None

    return OrderResult(
        success=True,
        order_id=order_id,
        status=status,
        filled_price=spread_credit,
        filled_price_source=filled_price_source,
        order_limit_price=order_limit_price,
        broker_aggregate_fill_price=broker_aggregate_fill_price,
        filled_quantity=filled_qty,
        order_quantity=order_qty or None,
        remaining_quantity=remaining,
        short_fill_price=short_fill,
        long_fill_price=long_fill,
        filled_at=filled_at,
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
        self._live_orders_cache: Optional[list] = None
        self._live_orders_ts: float = 0.0
        self._live_orders_ttl = float(os.environ.get('TT_LIVE_ORDERS_CACHE_TTL_SEC', '2'))
        self._last_broker_error: Optional[str] = None

    def _loop_worker(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

    def _run(self, coro, timeout: float = 120, *, priority: str = 'NORMAL', op: str = ''):
        """Thread-safe: monitors call broker from parallel threads."""
        from common.rest_metrics import record_429, record_failure, record_skipped_cooldown

        operation = op or 'broker'
        if should_skip_priority(priority):
            try:
                record_skipped_cooldown(operation, priority)
            except Exception:
                pass
            raise BrokerCooldownActive(f'cooldown active — skipped {operation}')
        get_rest_limiter().acquire(priority=priority, name=operation)
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            return future.result(timeout=timeout)
        except Exception as exc:
            try:
                record_failure(operation, exc)
                if '429' in str(exc).lower() or 'rate limit' in str(exc).lower():
                    record_429()
            except Exception:
                pass
            self._maybe_enter_cooldown(exc, op=operation)
            if '429' in str(exc).lower() or 'rate limit' in str(exc).lower():
                raise BrokerRateLimited(str(exc)) from exc
            raise

    def _maybe_enter_cooldown(self, exc: Exception, *, op: str = '') -> None:
        err = str(exc).lower()
        self._last_broker_error = str(exc)
        if any(tok in err for tok in ('401', '429', 'unauthorized', 'timeout', 'rate limit', 'blocked', 'forbidden')):
            set_cooldown(str(exc), source=op or 'tastytrade_broker')

    def broker_health(self) -> dict:
        from common.broker_cooldown import cooldown_snapshot
        from common.broker_factory import shared_broker_stats
        from common.rest_limiter import get_rest_limiter

        return {
            'last_error': self._last_broker_error,
            'rest': get_rest_limiter().stats(),
            'cooldown': cooldown_snapshot(),
            'shared_broker': shared_broker_stats(),
        }

    def get_live_orders_cached(self, ttl_sec: Optional[float] = None):
        ttl = self._live_orders_ttl if ttl_sec is None else ttl_sec
        now = time.time()
        if self._live_orders_cache is not None and now - self._live_orders_ts < ttl:
            return self._live_orders_cache
        orders = self._run(
            self.account.get_live_orders(self.session),
            priority='LOW',
            op='get_live_orders',
        )
        self._live_orders_cache = list(orders or [])
        self._live_orders_ts = now
        return self._live_orders_cache

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
        from common.trading_gate import effective_new_risk_blocked, gate_enabled

        if gate_enabled() and effective_new_risk_blocked():
            return OrderResult(
                False, None, 'rejected',
                message='new_risk_gate_blocked',
            )

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
            resp = _retry_on_transient(
                lambda: self._run(_build_and_place(), priority='HIGH', op='place_spread_order'),
            )
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
        self,
        short_symbol: str,
        long_symbol: str,
        qty: int,
        debit_limit: float,
        *,
        allow_unverified_emergency_close: bool = False,
    ) -> OrderResult:
        pos_state = self.inspect_spread_position(
            short_symbol, long_symbol, expected_qty=qty,
        )
        if pos_state != 'closable':
            if pos_state == 'unknown' and allow_unverified_emergency_close:
                log.warning(
                    'BROKER_PREFLIGHT_EMERGENCY_OVERRIDE short=%s long=%s qty=%s',
                    short_symbol,
                    long_symbol,
                    qty,
                )
            else:
                log.error(
                    'BROKER_PREFLIGHT_BLOCKED_SPREAD_CLOSE short=%s long=%s qty=%s reason=%s',
                    short_symbol,
                    long_symbol,
                    qty,
                    pos_state,
                )
                return OrderResult(
                    False,
                    None,
                    'rejected_preflight',
                    message=f'spread_not_closable_{pos_state}',
                    transmitted=False,
                )

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
            resp = _retry_on_transient(
                lambda: self._run(_build_and_place(), priority='HIGH', op='place_spread_close_order'),
            )
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
            resp = _retry_on_transient(
                lambda: self._run(_place(), priority='HIGH', op='place_order'),
            )
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
                lambda: self._run(
                    self.account.delete_order(self.session, int(order_id)),
                    priority='HIGH',
                    op='cancel_order',
                )
            )
            self._live_orders_cache = None
            return OrderResult(True, order_id, 'cancelled', message='Cancelled')
        except Exception as exc:
            msg = str(exc)
            if 'filled' in msg.lower():
                return OrderResult(True, order_id, 'filled', message=msg)
            if '401' in msg or 'unauthorized' in msg.lower():
                self.validate_session()
            return OrderResult(False, order_id, 'rejected', message=msg)

    def get_order_status(
        self,
        order_id: str,
        *,
        priority: str = 'NORMAL',
        op: str = 'get_order',
        live_orders: Optional[list] = None,
    ) -> OrderResult:
        """Resolve order status from an optional live-orders snapshot, else cache, else get_order.

        When ``live_orders`` is provided (batched peaceful reconcile), search that
        list first and only call direct ``get_order`` if the id is absent.
        """
        try:
            orders = live_orders if live_orders is not None else self.get_live_orders_cached()
            for o in orders or []:
                if str(getattr(o, 'id', None)) == str(order_id):
                    return _order_result_from_placed_order(o)
            order = self._run(
                self.account.get_order(self.session, int(order_id)),
                priority=priority,
                op=op,
            )
            return _order_result_from_placed_order(order)
        except BrokerCooldownActive:
            raise
        except BrokerRateLimited:
            raise
        except Exception as exc:
            return OrderResult(False, order_id, 'rejected', message=str(exc))

    def prime_live_orders_cache(self, orders: list) -> None:
        """Install a shared live-orders snapshot for subsequent cache hits."""
        self._live_orders_cache = list(orders or [])
        self._live_orders_ts = time.time()

    def get_order_status_direct(
        self,
        order_id: str,
        *,
        priority: str = 'HIGH',
        op: str = 'entry_open_order_status',
    ) -> OrderResult:
        """One direct get_order REST call — no live-orders cache."""
        try:
            order = self._run(
                self.account.get_order(self.session, int(order_id)),
                priority=priority,
                op=op,
            )
            return _order_result_from_placed_order(order)
        except BrokerCooldownActive:
            raise
        except Exception as exc:
            return OrderResult(False, order_id, 'rejected', message=str(exc))

    def probe_orders_rest(
        self,
        *,
        priority: str = 'HIGH',
        op: str = 'rest_health_probe_orders',
        bypass_local_cooldown: bool = False,
        timeout: float = 10.0,
    ):
        """One uncached get_live_orders call for REST health."""
        from common.rest_probe import RestProbeResult, classify_rest_exception

        attempted = time.time()
        if not bypass_local_cooldown and should_skip_priority(priority):
            raise BrokerCooldownActive(f'cooldown active — skipped {op}')
        get_rest_limiter().acquire(priority=priority, name=op)
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.account.get_live_orders(self.session),
                self._loop,
            )
            future.result(timeout=timeout)
            completed = time.time()
            return RestProbeResult(
                ok=True,
                status='healthy',
                attempted_at_epoch=attempted,
                completed_at_epoch=completed,
                latency_ms=int((completed - attempted) * 1000),
                http_status=200,
                detail='',
                operation=op,
            )
        except BrokerCooldownActive:
            raise
        except Exception as exc:
            try:
                from common.rest_metrics import record_429, record_failure

                record_failure(op, exc)
                if '429' in str(exc).lower() or 'rate limit' in str(exc).lower():
                    record_429()
            except Exception:
                pass
            self._maybe_enter_cooldown(exc, op=op)
            if '429' in str(exc).lower() or 'rate limit' in str(exc).lower():
                raise BrokerRateLimited(str(exc)) from exc
            status, http_status = classify_rest_exception(exc)
            completed = time.time()
            return RestProbeResult(
                ok=False,
                status=status,
                attempted_at_epoch=attempted,
                completed_at_epoch=completed,
                latency_ms=int((completed - attempted) * 1000),
                http_status=http_status,
                detail=str(exc),
                operation=op,
            )

    _ACTIVE_ORDER_STATUSES = frozenset(
        {'live', 'working', 'received', 'contingent', 'open', 'partially filled'}
    )

    def find_working_close_orders(self, symbol: str) -> List[OrderResult]:
        """Live BUY_TO_CLOSE orders on the short leg (stop, limit, or market)."""
        try:
            orders = self.get_live_orders_cached()
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

    def find_working_open_spread_orders(self, short_symbol: str, long_symbol: str) -> List[OrderResult]:
        """Live SELL_TO_OPEN/BUY_TO_OPEN spread orders matching symbols."""
        try:
            orders = self.get_live_orders_cached()
        except Exception as exc:
            log.warning('find_working_open_spread_orders failed: %s', exc)
            return []

        short_tt = to_tastytrade(short_symbol)
        long_tt = to_tastytrade(long_symbol)
        matches: List[OrderResult] = []
        for order in orders:
            status = str(getattr(order, 'status', '')).lower()
            if status not in self._ACTIVE_ORDER_STATUSES:
                continue
            legs = getattr(order, 'legs', None) or []
            if len(legs) != 2:
                continue
            short_hit = long_hit = False
            for leg in legs:
                leg_sym = str(getattr(leg, 'symbol', '') or '')
                action = _normalize_leg_action(getattr(leg, 'action', ''))
                if action == 'SELL_TO_OPEN' and symbols_equivalent(leg_sym, short_tt):
                    short_hit = True
                if action == 'BUY_TO_OPEN' and symbols_equivalent(leg_sym, long_tt):
                    long_hit = True
            if short_hit and long_hit:
                matches.append(_order_result_from_placed_order(order))
        return matches

    def inspect_spread_position(
        self,
        short_symbol: str,
        long_symbol: str,
        *,
        expected_qty: int,
    ) -> str:
        """F-9 — block spread close when account has no closable vertical."""
        try:
            positions = self._run(
                self.account.get_positions(self.session),
                priority='NORMAL',
                op='get_positions',
            )
        except Exception as exc:
            log.warning('inspect_spread_position unavailable: %s', exc)
            return 'unknown'

        short_tt = to_tastytrade(short_symbol)
        long_tt = to_tastytrade(long_symbol)
        short_qty = long_qty = 0
        for pos in positions or []:
            sym = getattr(pos, 'symbol', None) or getattr(pos, 'underlying_symbol', '')
            sym_s = str(sym)
            qty = _signed_position_qty(pos)
            if symbols_equivalent(sym_s, short_tt):
                short_qty = qty
            elif symbols_equivalent(sym_s, long_tt):
                long_qty = qty

        if short_qty == 0 and long_qty == 0:
            return 'flat'
        # Short leg of credit spread is short (negative qty at TT for short options)
        short_closable = abs(short_qty) >= expected_qty and short_qty < 0
        long_closable = long_qty >= expected_qty and long_qty > 0
        if short_closable and long_closable:
            return 'closable'
        if short_qty == 0 and long_qty == 0:
            return 'flat'
        return 'mismatch'

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
        from common.rest_operations import OPERATION_ENTRY_MARKET_DATA_REST

        # REST /market-data/by-type expects Schwab/OCC symbols, not DXLink streamer symbols.
        tt_keys = list(dict.fromkeys(to_tastytrade(s) for s in symbols if s))
        api_symbols = list(dict.fromkeys(to_schwab(s) for s in tt_keys))
        out: Dict[str, float] = {}
        for i in range(0, len(api_symbols), 100):
            batch = api_symbols[i:i + 100]
            try:
                items = _retry_on_transient(
                    lambda b=batch: self._run(
                        get_market_data_by_type(self.session, options=b),
                        priority='NORMAL',
                        op=OPERATION_ENTRY_MARKET_DATA_REST,
                    )
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
