"""Wrap a live broker so stop_monitor sees an exchange stop as filled (integration tests)."""
from __future__ import annotations

from brokers.base import BrokerBase, OrderResult


class SimulatedStopFillBroker(BrokerBase):
    """
    Delegate all broker calls to ``inner`` except ``get_order_status`` for one
    short-leg stop order id (Scenario 5: stop filled → long leg close).
    """

    def __init__(self, inner: BrokerBase, stop_order_id: str, *, quantity: int):
        self._inner = inner
        self._stop_order_id = str(stop_order_id)
        self._quantity = int(quantity)

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
        if str(order_id) == self._stop_order_id:
            return OrderResult(
                success=True,
                order_id=self._stop_order_id,
                status='filled',
                filled_quantity=self._quantity,
                order_quantity=self._quantity,
            )
        return self._inner.get_order_status(order_id)
