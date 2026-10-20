"""Journal paper broker — live market data, locally recorded fills (no broker orders)."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from blocks.stop.stop_math import round_spx_option_price
from brokers.base import BrokerBase, ComboLeg, OrderResult
from common.symbols import is_futures_symbol, symbols_equivalent, to_tastytrade

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
DEFAULT_JOURNAL_ORDERS_FILE = os.path.join(ROOT, 'trades', 'journal_orders.json')


def _dict_to_order_result(data: dict) -> OrderResult:
    return OrderResult(
        success=bool(data.get('success', True)),
        order_id=data.get('order_id'),
        status=data.get('status', 'unknown'),
        filled_price=data.get('filled_price'),
        filled_price_source=data.get('filled_price_source'),
        order_limit_price=data.get('order_limit_price'),
        broker_aggregate_fill_price=data.get('broker_aggregate_fill_price'),
        filled_quantity=data.get('filled_quantity'),
        order_quantity=data.get('order_quantity'),
        remaining_quantity=data.get('remaining_quantity'),
        short_fill_price=data.get('short_fill_price'),
        long_fill_price=data.get('long_fill_price'),
        filled_at=data.get('filled_at'),
        transmitted=bool(data.get('transmitted', False)),
        message=str(data.get('message', '')),
        raw=data,
    )


class JournalBroker(BrokerBase):
    """
    Wrap a live broker for quotes only. Orders are recorded locally at the
    requested limit/mid price and never sent to the exchange or tastyware.
    Orders are persisted to disk so multi-process architectures (launcher vs
    stop_monitor) can synchronize paper positions without hitting the broker.
    """

    _BLOCKED_LIVE_METHODS = frozenset({
        'execute_order',
        'send_order',
        'get_order_status_direct',
        '_build_and_place',
        '_place_single_leg_order',
        'cancel_complex_order',
    })

    def __init__(
        self,
        inner: BrokerBase,
        *,
        strategy_name: str = '',
        orders_file: Optional[str] = None,
    ):
        self._inner = inner
        self._strategy_name = strategy_name
        self._is_paper = True
        self._is_journal = True
        self._orders_file = orders_file or DEFAULT_JOURNAL_ORDERS_FILE
        self._orders: Dict[str, dict] = {}
        self._last_file_mtime: float = -1.0
        self._lock = threading.Lock()
        with self._lock:
            self._load_orders()

    def connect(self) -> bool:
        return self._inner.connect()

    def __getattr__(self, name: str) -> Any:
        if name in self._BLOCKED_LIVE_METHODS or name.startswith(('place_', 'cancel_', 'replace_')):
            raise RuntimeError(
                f'JournalBroker blocked call to live broker order mutation method in paper mode: {name}'
            )
        return getattr(self._inner, name)

    def _journal_id(self) -> str:
        return f'journal-{uuid.uuid4().hex[:12]}'

    def _load_orders(self) -> None:
        """Load orders from persistent JSON file if file exists and changed."""
        if not self._orders_file or not os.path.exists(self._orders_file):
            return
        try:
            mtime = os.path.getmtime(self._orders_file)
            if mtime == self._last_file_mtime:
                return
            with open(self._orders_file, 'r', encoding='utf-8') as f:
                disk_orders = json.load(f)
            if isinstance(disk_orders, dict):
                for k, v in disk_orders.items():
                    if k not in self._orders:
                        self._orders[k] = v
                    else:
                        self._orders[k].update(v)
                self._last_file_mtime = mtime
        except Exception as exc:
            log.warning('JournalBroker: failed loading orders from %s: %s', self._orders_file, exc)

    def _save_orders(self) -> None:
        """Atomically persist orders dictionary to disk."""
        if not self._orders_file:
            return
        directory = os.path.dirname(self._orders_file)
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self._orders, f, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            for attempt in range(8):
                try:
                    os.replace(tmp, self._orders_file)
                    self._last_file_mtime = os.path.getmtime(self._orders_file)
                    return
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(0.05 * (attempt + 1))
        except Exception as exc:
            log.warning('JournalBroker: failed saving orders to %s: %s', self._orders_file, exc)
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def _reconstruct_order_from_active_trades(self, order_id: str) -> Optional[dict]:
        """Self-heal: if an order begins with journal- but is missing from disk, check active trade JSONs."""
        from blocks.stop import state as state_mod

        for path in state_mod.iter_active_trade_paths():
            try:
                trade = state_mod.load_state(path)
            except Exception:
                continue

            # 1. Open spread order
            if (
                trade.get('open_order_id') == order_id
                or (trade.get('open_order') or {}).get('order_id') == order_id
                or trade.get('current_open_order_id') == order_id
            ):
                short_leg = trade.get('short_leg') or {}
                long_leg = trade.get('long_leg') or {}
                qty = int(trade.get('quantity') or 1)
                credit = float(
                    (trade.get('entry') or {}).get('limit_credit')
                    or (trade.get('entry') or {}).get('net_credit')
                    or 0.70
                )
                short_fill = float(short_leg.get('fill_price') or 0.0)
                long_fill = float(long_leg.get('fill_price') or 0.0)
                if short_fill <= 0 or long_fill <= 0:
                    long_fill = 0.05
                    short_fill = round_spx_option_price(credit + 0.05)

                order_data = {
                    'success': True,
                    'order_id': order_id,
                    'status': 'filled',
                    'kind': 'spread_open',
                    'action': 'SELL_TO_OPEN',
                    'short_symbol': short_leg.get('symbol'),
                    'long_symbol': long_leg.get('symbol'),
                    'filled_price': round(credit, 2),
                    'filled_price_source': 'broker_leg_math',
                    'order_limit_price': round(credit, 2),
                    'short_fill_price': short_fill,
                    'long_fill_price': long_fill,
                    'order_quantity': qty,
                    'filled_quantity': qty,
                    'remaining_quantity': 0,
                    'created_at': time.time(),
                    'filled_at': time.time(),
                    'transmitted': False,
                    'message': 'journal:spread_open (reconstructed)',
                }
                self._orders[order_id] = order_data
                self._save_orders()
                return order_data

            # 2. Active stop
            active_stop = trade.get('active_stop') or {}
            if active_stop.get('order_id') == order_id:
                short_sym = (trade.get('short_leg') or {}).get('symbol')
                qty = int(
                    active_stop.get('quantity')
                    or trade.get('stop_quantity')
                    or trade.get('quantity')
                    or 1
                )
                st = active_stop.get('status', 'working')
                order_data = {
                    'success': True,
                    'order_id': order_id,
                    'status': st,
                    'kind': 'stop' if active_stop.get('type') != 'OCO' else 'oco',
                    'action': 'BUY_TO_CLOSE',
                    'symbol': short_sym,
                    'stop_price': active_stop.get('stop_price'),
                    'limit_price': active_stop.get('limit_price'),
                    'profit_price': active_stop.get('profit_price'),
                    'order_limit_price': active_stop.get('limit_price'),
                    'order_quantity': qty,
                    'filled_quantity': qty if st == 'filled' else 0,
                    'remaining_quantity': 0 if st == 'filled' else qty,
                    'created_at': time.time(),
                    'filled_at': time.time() if st == 'filled' else None,
                    'transmitted': False,
                    'message': 'journal:stop (reconstructed)',
                }
                self._orders[order_id] = order_data
                self._save_orders()
                return order_data

            # 3. Long close order
            if trade.get('long_close_order_id') == order_id:
                long_sym = (trade.get('long_leg') or {}).get('symbol')
                qty = int(trade.get('stop_quantity') or trade.get('quantity') or 1)
                order_data = {
                    'success': True,
                    'order_id': order_id,
                    'status': 'filled',
                    'kind': 'limit',
                    'action': 'SELL_TO_CLOSE',
                    'symbol': long_sym,
                    'filled_price': float(trade.get('long_close_limit_price') or 0.05),
                    'filled_price_source': 'journal_mid',
                    'order_limit_price': float(trade.get('long_close_limit_price') or 0.05),
                    'order_quantity': qty,
                    'filled_quantity': qty,
                    'remaining_quantity': 0,
                    'created_at': time.time(),
                    'filled_at': time.time(),
                    'transmitted': False,
                    'message': 'journal:long_close (reconstructed)',
                }
                self._orders[order_id] = order_data
                self._save_orders()
                return order_data

            # 4. Spread close order
            if trade.get('spread_close_order_id') == order_id:
                short_leg = trade.get('short_leg') or {}
                long_leg = trade.get('long_leg') or {}
                qty = int(trade.get('quantity') or 1)
                exit_cfg = trade.get('exit_config') or {}
                debit_limit = float(exit_cfg.get('profit_target_debit') or 0.15)
                order_data = {
                    'success': True,
                    'order_id': order_id,
                    'status': 'working',
                    'kind': 'spread_close',
                    'action': 'BUY_TO_CLOSE',
                    'short_symbol': short_leg.get('symbol'),
                    'long_symbol': long_leg.get('symbol'),
                    'order_limit_price': debit_limit,
                    'debit_limit': debit_limit,
                    'order_quantity': qty,
                    'filled_quantity': 0,
                    'remaining_quantity': qty,
                    'created_at': time.time(),
                    'filled_at': None,
                    'transmitted': False,
                    'message': 'journal:spread_close (reconstructed)',
                }
                self._orders[order_id] = order_data
                self._save_orders()
                return order_data

        return None

    def place_stop_order(
        self, symbol: str, qty: int, stop_price: float, limit_price: float
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            stop_px = round_spx_option_price(float(stop_price))
            limit_px = round_spx_option_price(float(limit_price))
            data = {
                'success': True,
                'order_id': oid,
                'status': 'working',
                'kind': 'stop',
                'action': 'BUY_TO_CLOSE',
                'symbol': symbol,
                'stop_price': stop_px,
                'limit_price': limit_px,
                'order_limit_price': limit_px,
                'order_quantity': int(qty),
                'filled_quantity': 0,
                'remaining_quantity': int(qty),
                'created_at': time.time(),
                'filled_at': None,
                'transmitted': False,
                'message': f'journal:stop stop={stop_px} limit={limit_px}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): stop recorded working id=%s symbol=%s stop=%.2f limit=%.2f qty=%d',
                self._strategy_name or 'strategy',
                oid,
                symbol,
                stop_px,
                limit_px,
                qty,
            )
            return _dict_to_order_result(data)

    def place_oco_order(
        self,
        symbol: str,
        qty: int,
        *,
        profit_price: float,
        stop_price: float,
        limit_price: float,
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            stop_px = round_spx_option_price(float(stop_price))
            limit_px = round_spx_option_price(float(limit_price))
            profit_px = round_spx_option_price(float(profit_price))
            data = {
                'success': True,
                'order_id': oid,
                'status': 'working',
                'kind': 'oco',
                'action': 'BUY_TO_CLOSE',
                'symbol': symbol,
                'stop_price': stop_px,
                'limit_price': limit_px,
                'profit_price': profit_px,
                'order_limit_price': limit_px,
                'order_quantity': int(qty),
                'filled_quantity': 0,
                'remaining_quantity': int(qty),
                'created_at': time.time(),
                'filled_at': None,
                'transmitted': False,
                'message': f'journal:oco profit={profit_px} stop={stop_px} limit={limit_px}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): oco recorded working id=%s symbol=%s profit=%.2f stop=%.2f limit=%.2f qty=%d',
                self._strategy_name or 'strategy',
                oid,
                symbol,
                profit_px,
                stop_px,
                limit_px,
                qty,
            )
            return _dict_to_order_result(data)

    def place_limit_order(
        self, action: str, symbol: str, qty: int, price: float
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            px = round(float(price), 2)
            data = {
                'success': True,
                'order_id': oid,
                'status': 'filled',
                'kind': 'limit',
                'action': action,
                'symbol': symbol,
                'filled_price': px,
                'filled_price_source': 'journal_mid',
                'order_limit_price': px,
                'order_quantity': int(qty),
                'filled_quantity': int(qty),
                'remaining_quantity': 0,
                'created_at': time.time(),
                'filled_at': time.time(),
                'transmitted': False,
                'message': f'journal:limit {action} @ {px}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): limit %s recorded filled id=%s symbol=%s price=%.2f qty=%d',
                self._strategy_name or 'strategy',
                action,
                oid,
                symbol,
                px,
                qty,
            )
            return _dict_to_order_result(data)

    def get_future_price(self, symbol: str = 'ES') -> Optional[float]:
        if hasattr(self._inner, 'get_future_price'):
            try:
                p = self._inner.get_future_price(symbol)
                if p is not None and p > 0:
                    return float(p)
            except Exception:
                pass
        from strategies.drma_hm.market_data_feed import fetch_es_current_price
        return fetch_es_current_price(broker=self._inner, allow_synthetic_fallback=False)

    def place_market_order(self, action: str, symbol: str, qty: int) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            px = 0.05
            if is_futures_symbol(symbol):
                p = self.get_future_price(symbol)
                if p is not None and p > 0:
                    px = round(float(p), 2)
            else:
                try:
                    p = self.get_option_price(symbol)
                    if p is not None and p > 0:
                        px = round(float(p), 2)
                except Exception:
                    pass

            data = {
                'success': True,
                'order_id': oid,
                'status': 'filled',
                'kind': 'market',
                'action': action,
                'symbol': symbol,
                'filled_price': px,
                'filled_price_source': 'journal_mid',
                'order_quantity': int(qty),
                'filled_quantity': int(qty),
                'remaining_quantity': 0,
                'created_at': time.time(),
                'filled_at': time.time(),
                'transmitted': False,
                'message': f'journal:market {action} @ {px}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): market %s recorded filled id=%s symbol=%s price=%.2f qty=%d',
                self._strategy_name or 'strategy',
                action,
                oid,
                symbol,
                px,
                qty,
            )
            return _dict_to_order_result(data)

    def place_spread_order(
        self, short_symbol: str, long_symbol: str, qty: int, credit: float
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            credit_f = round(float(credit), 2)
            short_p = None
            long_p = None
            try:
                short_p = self.get_option_price(short_symbol)
                long_p = self.get_option_price(long_symbol)
            except Exception:
                pass

            if short_p is not None and long_p is not None and float(short_p) > float(long_p) > 0:
                short_fill = round(float(short_p), 2)
                long_fill = max(0.05, round(short_fill - credit_f, 2))
                short_fill = round(long_fill + credit_f, 2)
            else:
                long_fill = 0.05
                short_fill = round(credit_f + 0.05, 2)

            data = {
                'success': True,
                'order_id': oid,
                'status': 'filled',
                'kind': 'spread_open',
                'action': 'SELL_TO_OPEN',
                'short_symbol': short_symbol,
                'long_symbol': long_symbol,
                'filled_price': credit_f,
                'filled_price_source': 'broker_leg_math',
                'order_limit_price': credit_f,
                'short_fill_price': short_fill,
                'long_fill_price': long_fill,
                'order_quantity': int(qty),
                'filled_quantity': int(qty),
                'remaining_quantity': 0,
                'created_at': time.time(),
                'filled_at': time.time(),
                'transmitted': False,
                'message': f'journal:spread_open short={short_fill} long={long_fill}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): spread_open recorded id=%s credit=%.2f short_px=%.2f long_px=%.2f qty=%d',
                self._strategy_name or 'strategy',
                oid,
                credit_f,
                short_fill,
                long_fill,
                qty,
            )
            return _dict_to_order_result(data)

    def place_multi_leg_order(
        self,
        legs: List[ComboLeg],
        *,
        price: float,
        order_type: str = 'LIMIT',
        time_in_force: str = 'DAY',
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            px = round(abs(float(price)), 2)
            data = {
                'success': True,
                'order_id': oid,
                'status': 'filled',
                'kind': f'multi_leg:{len(legs)}',
                'filled_price': px,
                'filled_price_source': 'journal_mid',
                'order_limit_price': px,
                'order_quantity': 1,
                'filled_quantity': 1,
                'remaining_quantity': 0,
                'created_at': time.time(),
                'filled_at': time.time(),
                'transmitted': False,
                'message': f'journal:multi_leg legs={len(legs)} limit={price}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): multi_leg recorded id=%s legs=%d price=%.2f',
                self._strategy_name or 'strategy',
                oid,
                len(legs),
                px,
            )
            return _dict_to_order_result(data)

    def place_spread_close_order(
        self,
        short_symbol: str,
        long_symbol: str,
        qty: int,
        debit_limit: float,
        *,
        allow_unverified_emergency_close: bool = False,
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            debit_f = round(float(debit_limit), 2)
            long_fill = 0.05
            short_fill = round(debit_f + 0.05, 2)

            short_p = None
            long_p = None
            try:
                short_p = self.get_option_price(short_symbol)
                long_p = self.get_option_price(long_symbol)
            except Exception:
                pass

            immediate_fill = allow_unverified_emergency_close
            if short_p is not None and long_p is not None:
                market_debit = max(0.0, round(float(short_p) - float(long_p), 2))
                if market_debit <= debit_f:
                    immediate_fill = True
            elif allow_unverified_emergency_close:
                immediate_fill = True
            else:
                immediate_fill = False

            status = 'filled' if immediate_fill else 'working'
            data = {
                'success': True,
                'order_id': oid,
                'status': status,
                'kind': 'spread_close',
                'action': 'BUY_TO_CLOSE',
                'short_symbol': short_symbol,
                'long_symbol': long_symbol,
                'filled_price': debit_f if status == 'filled' else None,
                'filled_price_source': 'broker_leg_math' if status == 'filled' else None,
                'order_limit_price': debit_f,
                'debit_limit': debit_f,
                'short_fill_price': short_fill if status == 'filled' else None,
                'long_fill_price': long_fill if status == 'filled' else None,
                'order_quantity': int(qty),
                'filled_quantity': int(qty) if status == 'filled' else 0,
                'remaining_quantity': 0 if status == 'filled' else int(qty),
                'created_at': time.time(),
                'filled_at': time.time() if status == 'filled' else None,
                'transmitted': False,
                'message': f'journal:spread_close debit={debit_f}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): spread_close recorded %s id=%s debit=%.2f qty=%d',
                self._strategy_name or 'strategy',
                status,
                oid,
                debit_f,
                qty,
            )
            return _dict_to_order_result(data)

    def place_spread_oco_order(
        self,
        short_symbol: str,
        long_symbol: str,
        qty: int,
        *,
        profit_debit: float,
        stop_trigger_debit: float,
        stop_limit_debit: float,
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = self._journal_id()
            profit_d = round_spx_option_price(float(profit_debit))
            stop_trig = round_spx_option_price(float(stop_trigger_debit))
            stop_lim = round_spx_option_price(float(stop_limit_debit))
            data = {
                'success': True,
                'order_id': oid,
                'status': 'working',
                'kind': 'spread_oco',
                'action': 'BUY_TO_CLOSE',
                'short_symbol': short_symbol,
                'long_symbol': long_symbol,
                'profit_debit': profit_d,
                'stop_trigger_debit': stop_trig,
                'stop_limit_debit': stop_lim,
                'order_limit_price': stop_lim,
                'order_quantity': int(qty),
                'filled_quantity': 0,
                'remaining_quantity': int(qty),
                'created_at': time.time(),
                'filled_at': None,
                'transmitted': False,
                'message': f'journal:spread_oco profit={profit_d} stop={stop_trig}',
            }
            self._orders[oid] = data
            self._save_orders()
            log.info(
                'Journal paper (%s): spread_oco recorded working id=%s profit=%.2f stop=%.2f qty=%d',
                self._strategy_name or 'strategy',
                oid,
                profit_d,
                stop_trig,
                qty,
            )
            return _dict_to_order_result(data)

    def replace_order(self, order_id: str, new_spec: Dict) -> OrderResult:
        self.cancel_order(order_id)
        action = new_spec.get('action', 'BUY_TO_CLOSE')
        symbol = new_spec.get('symbol', '')
        qty = int(new_spec.get('quantity', 1))
        price = float(new_spec.get('price', 1.0))
        return self.place_limit_order(action, symbol, qty, price)

    def cancel_order(self, order_id: str) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = str(order_id)
            if oid in self._orders:
                self._orders[oid]['status'] = 'cancelled'
                self._orders[oid]['message'] = 'journal:cancelled'
                self._save_orders()
            log.info('Journal paper: order %s cancelled', oid)
            return OrderResult(True, oid, 'cancelled', transmitted=False, message='journal:cancelled')

    def get_order_status(
        self,
        order_id: str,
        *,
        priority: str = 'NORMAL',
        op: str = 'get_order',
        live_orders: Any = None,
    ) -> OrderResult:
        with self._lock:
            self._load_orders()
            oid = str(order_id)
            data = self._orders.get(oid)
            if not data:
                data = self._reconstruct_order_from_active_trades(oid)
            if not data:
                return OrderResult(False, oid, 'unknown', message='journal:unknown_order')

            if data.get('status') == 'working':
                short_sym = data.get('short_symbol')
                long_sym = data.get('long_symbol')
                if short_sym and long_sym and data.get('kind') == 'spread_close':
                    try:
                        sp = self.get_option_price(short_sym)
                        lp = self.get_option_price(long_sym)
                    except Exception:
                        sp, lp = None, None
                    if sp is not None and lp is not None:
                        curr_debit = max(0.0, round(float(sp) - float(lp), 2))
                        debit_lim = data.get('order_limit_price') or data.get('debit_limit')
                        if debit_lim is not None and curr_debit <= float(debit_lim):
                            data['status'] = 'filled'
                            data['filled_price'] = float(debit_lim)
                            data['filled_price_source'] = 'broker_leg_math'
                            data['short_fill_price'] = round(float(debit_lim) + 0.05, 2)
                            data['long_fill_price'] = 0.05
                            data['filled_quantity'] = int(data.get('order_quantity') or 1)
                            data['remaining_quantity'] = 0
                            data['filled_at'] = time.time()
                            data['message'] = f'journal:spread_close_triggered debit={curr_debit} <= {debit_lim}'
                            self._save_orders()
                            log.info(
                                'Journal paper: spread close %s triggered for %s/%s at %.2f (limit=%.2f)',
                                oid,
                                short_sym,
                                long_sym,
                                curr_debit,
                                debit_lim,
                            )

                sym = data.get('symbol')
                if sym:
                    try:
                        current_p = self.get_option_price(sym)
                    except Exception:
                        current_p = None

                    if current_p is not None and current_p > 0:
                        stop_px = data.get('stop_price')
                        profit_px = data.get('profit_price')
                        # Check stop breach: for short option, rising price >= stop_price triggers stop
                        if stop_px is not None and current_p >= float(stop_px):
                            data['status'] = 'filled'
                            data['filled_price'] = float(data.get('limit_price') or current_p)
                            data['filled_quantity'] = int(data.get('order_quantity') or 1)
                            data['remaining_quantity'] = 0
                            data['filled_at'] = time.time()
                            data['message'] = f'journal:stop_triggered price={current_p} >= {stop_px}'
                            self._save_orders()
                            log.info(
                                'Journal paper: stop %s triggered for %s at %.2f (stop=%.2f)',
                                oid,
                                sym,
                                current_p,
                                stop_px,
                            )
                        # Check profit target: for short option, falling price <= profit_price triggers profit target
                        elif profit_px is not None and current_p <= float(profit_px):
                            data['status'] = 'filled'
                            data['filled_price'] = float(profit_px)
                            data['filled_quantity'] = int(data.get('order_quantity') or 1)
                            data['remaining_quantity'] = 0
                            data['filled_at'] = time.time()
                            data['message'] = f'journal:oco_profit_triggered price={current_p} <= {profit_px}'
                            self._save_orders()
                            log.info(
                                'Journal paper: OCO profit %s triggered for %s at %.2f (target=%.2f)',
                                oid,
                                sym,
                                current_p,
                                profit_px,
                            )

            return _dict_to_order_result(data)

    def get_order_status_direct(
        self,
        order_id: str,
        *,
        priority: str = 'NORMAL',
        op: str = 'get_order_status_direct',
    ) -> OrderResult:
        """Paper status is resolved purely locally; never delegates to live broker."""
        return self.get_order_status(order_id, priority=priority, op=op)

    def get_single_leg_position_qty(self, symbol: str) -> int:
        """
        Return net signed quantity held for symbol (short < 0, long > 0, 0 if flat)
        derived from active paper trade JSONs.
        """
        from blocks.stop import state as state_mod

        total_qty = 0
        sym_tt = to_tastytrade(symbol)
        for path in state_mod.iter_active_trade_paths():
            try:
                trade = state_mod.load_state(path)
            except Exception:
                continue

            status = str(trade.get('status') or '').lower()
            if status not in ('open', 'closing', 'pending_fill'):
                continue
            qty = int(trade.get('filled_quantity') or trade.get('quantity') or 0)
            if qty <= 0:
                continue

            short_sym = (trade.get('short_leg') or {}).get('symbol') or ''
            long_sym = (trade.get('long_leg') or {}).get('symbol') or ''

            if symbols_equivalent(short_sym, symbol) or symbols_equivalent(short_sym, sym_tt):
                if not trade.get('short_closed_at'):
                    total_qty -= qty

            if symbols_equivalent(long_sym, symbol) or symbols_equivalent(long_sym, sym_tt):
                if not trade.get('long_closed_at') and status != 'closed':
                    total_qty += qty

        return total_qty

    def find_working_close_orders(self, symbol: str) -> List[OrderResult]:
        with self._lock:
            self._load_orders()
            matches = []
            sym_tt = to_tastytrade(symbol)
            for data in self._orders.values():
                status = str(data.get('status') or '').lower()
                if status not in ('working', 'live', 'open'):
                    continue
                order_sym = data.get('symbol') or ''
                if symbols_equivalent(order_sym, symbol) or symbols_equivalent(order_sym, sym_tt):
                    matches.append(_dict_to_order_result(data))
            return matches

    def find_working_close_order(self, symbol: str) -> Optional[OrderResult]:
        matches = self.find_working_close_orders(symbol)
        return matches[0] if matches else None

    def find_working_open_spread_orders(
        self, short_symbol: str, long_symbol: str
    ) -> List[OrderResult]:
        with self._lock:
            self._load_orders()
            matches = []
            short_tt = to_tastytrade(short_symbol)
            long_tt = to_tastytrade(long_symbol)
            for data in self._orders.values():
                status = str(data.get('status') or '').lower()
                if status not in ('working', 'live', 'open'):
                    continue
                if data.get('kind') == 'spread_open':
                    s_sym = data.get('short_symbol') or ''
                    l_sym = data.get('long_symbol') or ''
                    if (
                        symbols_equivalent(s_sym, short_symbol)
                        or symbols_equivalent(s_sym, short_tt)
                    ) and (
                        symbols_equivalent(l_sym, long_symbol)
                        or symbols_equivalent(l_sym, long_tt)
                    ):
                        matches.append(_dict_to_order_result(data))
            return matches

    def get_live_orders_cached(self, ttl_sec: float = 0) -> List[OrderResult]:
        with self._lock:
            self._load_orders()
            return [
                _dict_to_order_result(d)
                for d in self._orders.values()
                if str(d.get('status') or '').lower() in ('working', 'live', 'open')
            ]

    def inspect_spread_position(
        self, short_symbol: str, long_symbol: str, *, expected_qty: int
    ) -> str:
        return 'closable'

    def close_at_market(self, symbol: str, qty: int) -> OrderResult:
        return self.place_market_order('BUY_TO_CLOSE', symbol, qty)

    def get_option_price(self, symbol: str) -> Optional[float]:
        return self._inner.get_option_price(symbol)

    def get_spx_price(self) -> Optional[float]:
        return self._inner.get_spx_price()

    def fetch_spx_price_api(self) -> Optional[float]:
        return self._inner.fetch_spx_price_api()

    def fetch_option_mids_api(self, symbols: List[str]) -> Dict[str, float]:
        return self._inner.fetch_option_mids_api(symbols)

    def get_future_price(self, symbol: str = 'ES') -> Optional[float]:
        if hasattr(self._inner, 'get_future_price'):
            p = self._inner.get_future_price(symbol)
            if p is not None and p > 0:
                return p
        if self._prices is not None:
            clean = symbol.strip().upper().lstrip('/')
            for s in (clean, f'/{clean}', f'/{clean}:XCME'):
                p = self._prices.get(s)
                if p is not None and p > 0:
                    return round(float(p), 2)
        return None

