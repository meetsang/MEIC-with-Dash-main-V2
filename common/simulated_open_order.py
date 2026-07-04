"""Wrap a live broker so fill_sync sees a simulated open-order leg-in sequence."""
from __future__ import annotations

from brokers.base import BrokerBase, OrderResult


class SimulatedOpenOrderBroker(BrokerBase):
    """
    Delegate all broker calls to ``inner`` except ``get_order_status`` for one
    open spread order id (Scenario 4 integration tests without a live partial fill).
    """

    def __init__(
        self,
        inner: BrokerBase,
        open_order_id: str,
        *,
        quantity: int,
        short_fill: float,
        long_fill: float,
        credit: float,
        step: int = 1,
        partial_qty: int | None = None,
    ):
        self._inner = inner
        self._open_order_id = str(open_order_id)
        self._quantity = int(quantity)
        self._short_fill = float(short_fill)
        self._long_fill = float(long_fill)
        self._credit = float(credit)
        self._step = int(step)
        self._partial_qty = int(partial_qty) if partial_qty is not None else max(1, self._quantity // 2)

    def connect(self) -> bool:
        return self._inner.connect()

    def place_stop_order(self, symbol, qty, stop_price, limit_price) -> OrderResult:
        return self._inner.place_stop_order(symbol, qty, stop_price, limit_price)

    def place_limit_order(self, action, symbol, qty, price) -> OrderResult:
        return self._inner.place_limit_order(action, symbol, qty, price)

    def place_market_order(self, action, symbol, qty) -> OrderResult:
        return self._inner.place_market_order(action, symbol, qty)

    def place_spread_order(self, short_symbol, long_symbol, qty, credit) -> OrderResult:
        return self._inner.place_spread_order(short_symbol, long_symbol, qty, credit)

    def place_spread_close_order(self, short_symbol, long_symbol, qty, debit_limit) -> OrderResult:
        return self._inner.place_spread_close_order(short_symbol, long_symbol, qty, debit_limit)

    def replace_order(self, order_id, new_spec) -> OrderResult:
        return self._inner.replace_order(order_id, new_spec)

    def cancel_order(self, order_id) -> OrderResult:
        return self._inner.cancel_order(order_id)

    def get_option_price(self, symbol):
        return self._inner.get_option_price(symbol)

    def get_spx_price(self):
        return self._inner.get_spx_price()

    def get_order_status(self, order_id: str) -> OrderResult:
        if str(order_id) != self._open_order_id:
            return self._inner.get_order_status(order_id)
        return self._simulated_open_status()

    def _simulated_open_status(self) -> OrderResult:
        if self._step <= 1:
            filled = min(self._partial_qty, self._quantity)
            return OrderResult(
                success=True,
                order_id=self._open_order_id,
                status='partial',
                filled_quantity=filled,
                order_quantity=self._quantity,
                filled_price=self._credit,
                short_fill_price=self._short_fill,
                long_fill_price=self._long_fill,
            )
        return OrderResult(
            success=True,
            order_id=self._open_order_id,
            status='filled',
            filled_quantity=self._quantity,
            order_quantity=self._quantity,
            filled_price=self._credit,
            short_fill_price=self._short_fill,
            long_fill_price=self._long_fill,
        )
