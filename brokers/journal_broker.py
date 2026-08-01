"""Journal paper broker — live market data, locally recorded fills (no broker orders)."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Dict, List, Optional

from brokers.base import BrokerBase, ComboLeg, OrderResult

log = logging.getLogger(__name__)


class JournalBroker(BrokerBase):
    """
    Wrap a live broker for quotes only. Orders are recorded locally at the
    requested limit/mid price and never sent to the exchange or tastyware.
    """

    def __init__(self, inner: BrokerBase, *, strategy_name: str = ''):
        self._inner = inner
        self._strategy_name = strategy_name
        self._is_paper = True
        self._is_journal = True
        self._orders: Dict[str, OrderResult] = {}

    def connect(self) -> bool:
        return self._inner.connect()

    def _journal_id(self) -> str:
        return f'journal-{uuid.uuid4().hex[:12]}'

    def _record(
        self,
        *,
        kind: str,
        filled_price: Optional[float] = None,
        filled_quantity: int = 1,
        message: str = '',
    ) -> OrderResult:
        oid = self._journal_id()
        result = OrderResult(
            True,
            oid,
            'filled',
            filled_price=filled_price,
            filled_price_source='journal_mid',
            filled_quantity=filled_quantity,
            transmitted=False,
            message=message or f'journal:{kind}',
            filled_at=time.time(),
        )
        self._orders[oid] = result
        log.info(
            'Journal paper (%s): %s recorded locally id=%s price=%s qty=%s',
            self._strategy_name or 'strategy',
            kind,
            oid,
            filled_price,
            filled_quantity,
        )
        return result

    def place_stop_order(self, symbol, qty, stop_price, limit_price) -> OrderResult:
        return self._record(kind='stop', filled_price=limit_price, filled_quantity=qty)

    def place_limit_order(self, action, symbol, qty, price) -> OrderResult:
        return self._record(kind='limit', filled_price=price, filled_quantity=qty)

    def place_market_order(self, action, symbol, qty) -> OrderResult:
        return self._record(kind='market', filled_quantity=qty)

    def place_spread_order(self, short_symbol, long_symbol, qty, credit) -> OrderResult:
        return self._record(kind='spread_open', filled_price=credit, filled_quantity=qty)

    def place_multi_leg_order(
        self,
        legs: List[ComboLeg],
        *,
        price: float,
        order_type: str = 'LIMIT',
        time_in_force: str = 'DAY',
    ) -> OrderResult:
        return self._record(
            kind=f'multi_leg:{len(legs)}',
            filled_price=abs(price),
            filled_quantity=1,
            message=f'journal:multi_leg legs={len(legs)} limit={price}',
        )

    def place_spread_close_order(
        self,
        short_symbol,
        long_symbol,
        qty,
        debit_limit,
        *,
        allow_unverified_emergency_close: bool = False,
    ) -> OrderResult:
        return self._record(
            kind='spread_close',
            filled_price=debit_limit,
            filled_quantity=qty,
        )

    def replace_order(self, order_id: str, new_spec: Dict) -> OrderResult:
        prior = self._orders.get(str(order_id))
        price = new_spec.get('price') or (prior.filled_price if prior else None)
        return self._record(kind='replace', filled_price=price)

    def cancel_order(self, order_id: str) -> OrderResult:
        oid = str(order_id)
        if oid in self._orders:
            self._orders[oid] = OrderResult(
                True, oid, 'cancelled', transmitted=False, message='journal:cancelled'
            )
        return OrderResult(True, oid, 'cancelled', transmitted=False)

    def get_order_status(
        self,
        order_id: str,
        *,
        priority: str = 'NORMAL',
        op: str = 'get_order',
    ) -> OrderResult:
        oid = str(order_id)
        if oid in self._orders:
            return self._orders[oid]
        return OrderResult(False, oid, 'unknown', message='journal:unknown_order')

    def get_option_price(self, symbol: str) -> Optional[float]:
        return self._inner.get_option_price(symbol)

    def get_spx_price(self) -> Optional[float]:
        return self._inner.get_spx_price()

    def fetch_spx_price_api(self) -> Optional[float]:
        return self._inner.fetch_spx_price_api()

    def fetch_option_mids_api(self, symbols: List[str]) -> Dict[str, float]:
        return self._inner.fetch_option_mids_api(symbols)

    def inspect_spread_position(self, short_symbol, long_symbol, *, expected_qty: int) -> str:
        return 'closable'
