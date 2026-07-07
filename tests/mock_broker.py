"""Mock broker for offline unit tests."""
from __future__ import annotations

from typing import Dict, Optional

from brokers.base import BrokerBase, OrderResult
from common.symbols import to_tastytrade


class MockBroker(BrokerBase):
  def __init__(self):
    self._connected = False
    self._order_seq = 1000
    self.orders: Dict[str, OrderResult] = {}
    self.prices: Dict[str, float] = {'SPX': 5500.0}
    self.placed: list = []
    self.spread_position_flat: bool = False

  def connect(self) -> bool:
    self._connected = True
    return True

  def _next_id(self) -> str:
    self._order_seq += 1
    return str(self._order_seq)

  def place_stop_order(self, symbol, qty, stop_price, limit_price) -> OrderResult:
    oid = self._next_id()
    r = OrderResult(True, oid, 'working')
    self.orders[oid] = r
    self.placed.append(('stop', symbol, stop_price))
    return r

  def place_limit_order(self, action, symbol, qty, price) -> OrderResult:
    oid = self._next_id()
    r = OrderResult(True, oid, 'working')
    self.orders[oid] = r
    self.placed.append(('limit', symbol, price))
    return r

  def place_market_order(self, action, symbol, qty) -> OrderResult:
    oid = self._next_id()
    r = OrderResult(True, oid, 'filled', filled_price=1.0, filled_quantity=qty)
    self.orders[oid] = r
    self.placed.append(('market', symbol, None))
    return r

  def place_spread_order(self, short_symbol, long_symbol, qty, credit) -> OrderResult:
    oid = self._next_id()
    r = OrderResult(True, oid, 'filled', filled_price=credit, filled_quantity=qty)
    self.orders[oid] = r
    self.placed.append(('spread', short_symbol, credit))
    return r

  def place_spread_close_order(self, short_symbol, long_symbol, qty, debit_limit) -> OrderResult:
    if self.spread_position_flat:
      return OrderResult(
          False, None, 'rejected_preflight',
          message='spread_not_closable_flat',
          transmitted=False,
      )
    oid = self._next_id()
    r = OrderResult(
        True, oid, 'filled',
        filled_price=debit_limit,
        filled_quantity=qty,
        short_fill_price=debit_limit + 0.5,
        long_fill_price=0.5,
    )
    self.orders[oid] = r
    self.placed.append(('spread_close', short_symbol, long_symbol, debit_limit))
    return r

  def replace_order(self, order_id, new_spec) -> OrderResult:
    return self.place_limit_order('BUY_TO_CLOSE', new_spec.get('symbol', ''), 1, 1.0)

  def cancel_order(self, order_id) -> OrderResult:
    if order_id in self.orders:
      self.orders[order_id] = OrderResult(True, order_id, 'cancelled')
    return OrderResult(True, order_id, 'cancelled')

  def get_order_status(self, order_id) -> OrderResult:
    if order_id not in self.orders:
      return OrderResult(False, order_id, 'unknown')
    return self.orders[order_id]

  def find_working_close_orders(self, symbol) -> list:
    return []

  def find_working_close_order(self, symbol):
    return None

  def get_option_price(self, symbol) -> Optional[float]:
    return self.prices.get(symbol, 0.50)

  def get_spx_price(self) -> Optional[float]:
    return self.prices.get('SPX')

  def fetch_spx_price_api(self) -> Optional[float]:
    return self.get_spx_price()

  def fetch_option_mids_api(self, symbols) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym in symbols:
      tt = to_tastytrade(sym)
      price = self.prices.get(sym) or self.prices.get(tt)
      if price is not None:
        out[tt] = price
    return out

  def inspect_spread_position(self, short_symbol, long_symbol, *, expected_qty) -> str:
    if self.spread_position_flat:
      return 'flat'
    return 'closable'
