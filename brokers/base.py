"""Broker abstraction for order execution and market data."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str]
    status: str  # filled, working, rejected, cancelled, partial
    filled_price: Optional[float] = None
    filled_quantity: Optional[int] = None
    order_quantity: Optional[int] = None
    remaining_quantity: Optional[int] = None
    short_fill_price: Optional[float] = None
    long_fill_price: Optional[float] = None
    filled_at: Optional[float] = None  # unix timestamp of broker fill (R-9)
    transmitted: bool = True
    message: str = ''
    raw: Any = field(default=None, repr=False)


class BrokerBase(ABC):
    """Unified broker interface for strategies and stop_monitor."""

    @abstractmethod
    def connect(self) -> bool:
        ...

    @abstractmethod
    def place_stop_order(
        self, symbol: str, qty: int, stop_price: float, limit_price: float
    ) -> OrderResult:
        ...

    @abstractmethod
    def place_limit_order(
        self, action: str, symbol: str, qty: int, price: float
    ) -> OrderResult:
        ...

    @abstractmethod
    def place_market_order(self, action: str, symbol: str, qty: int) -> OrderResult:
        ...

    @abstractmethod
    def place_spread_order(
        self, short_symbol: str, long_symbol: str, qty: int, credit: float
    ) -> OrderResult:
        ...

    @abstractmethod
    def place_spread_close_order(
        self,
        short_symbol: str,
        long_symbol: str,
        qty: int,
        debit_limit: float,
        *,
        allow_unverified_emergency_close: bool = False,
    ) -> OrderResult:
        ...

    @abstractmethod
    def replace_order(self, order_id: str, new_spec: Dict) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        ...

    @abstractmethod
    def get_order_status(self, order_id: str) -> OrderResult:
        ...

    @abstractmethod
    def get_option_price(self, symbol: str) -> Optional[float]:
        ...

    @abstractmethod
    def get_spx_price(self) -> Optional[float]:
        ...

    def fetch_spx_price_api(self) -> Optional[float]:
        """REST market-data SPX (scan path — no MQTT streamer required)."""
        return None

    def fetch_option_mids_api(self, symbols: List[str]) -> Dict[str, float]:
        """REST market-data mids for option symbols (batch, scan path)."""
        return {}

    def inspect_spread_position(
        self,
        short_symbol: str,
        long_symbol: str,
        *,
        expected_qty: int,
    ) -> str:
        """
        Pre-close position check. Returns one of:
        closable | flat | mismatch | not_closable | unknown
        """
        return 'closable'

    def close_at_market(self, symbol: str, qty: int) -> OrderResult:
        return self.place_market_order('BUY_TO_CLOSE', symbol, qty)
