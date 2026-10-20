"""Order construction, tick rounding, and status normalization for Iron Fly."""
from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Any, Dict, List, Optional, Tuple

from brokers.base import ComboLeg
from common.symbols import build_schwab_symbol, build_tastytrade_symbol
from strategies.iron_fly.rules import build_closing_legs, build_fly_legs


def round_credit_down(val: float, tick: float = 0.05) -> float:
    """Round credit price down to the nearest combo tick (default 0.05)."""
    d_val = Decimal(str(round(val, 4)))
    d_tick = Decimal(str(tick))
    rounded = (d_val / d_tick).quantize(Decimal('1'), rounding=ROUND_DOWN) * d_tick
    return float(rounded)


def round_debit_up(val: float, tick: float = 0.05) -> float:
    """Round debit price up to the nearest combo tick (default 0.05)."""
    d_val = Decimal(str(round(val, 4)))
    d_tick = Decimal(str(tick))
    rounded = (d_val / d_tick).quantize(Decimal('1'), rounding=ROUND_UP) * d_tick
    return float(rounded)


def build_iron_fly_open_spec(
    expiry_yymmdd: str,
    center: float,
    cw: int,
    pw: int,
    limit_credit: float,
    qty: int = 1,
    combo_tick: float = 0.05,
    broker_format: str = 'tastytrade',
) -> Tuple[List[ComboLeg], float, Dict[str, Any]]:
    """Build open order legs, rounded credit, and standard order spec JSON."""
    price = round_credit_down(limit_credit, combo_tick)
    legs = build_fly_legs(expiry_yymmdd, center, cw, pw, qty=qty, broker_format=broker_format)

    schwab_legs = [
        {"instruction": "SELL_TO_OPEN", "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'C', int(center)), "assetType": "OPTION"}},
        {"instruction": "SELL_TO_OPEN", "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'P', int(center)), "assetType": "OPTION"}},
        {"instruction": "BUY_TO_OPEN",  "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'C', int(center + cw)), "assetType": "OPTION"}},
        {"instruction": "BUY_TO_OPEN",  "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'P', int(center - pw)), "assetType": "OPTION"}},
    ]

    order_spec = {
        "orderType": "NET_CREDIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": f"{price:.2f}",
        "orderLegCollection": schwab_legs,
    }

    return legs, price, order_spec


def build_iron_fly_close_spec(
    expiry_yymmdd: str,
    center: float,
    cw: int,
    pw: int,
    limit_debit: float,
    qty: int = 1,
    combo_tick: float = 0.05,
    broker_format: str = 'tastytrade',
) -> Tuple[List[ComboLeg], float, Dict[str, Any]]:
    """Build close order legs, rounded debit, and standard order spec JSON."""
    price = round_debit_up(limit_debit, combo_tick)
    legs = build_closing_legs(expiry_yymmdd, center, cw, pw, qty=qty, broker_format=broker_format)

    schwab_legs = [
        {"instruction": "BUY_TO_CLOSE",  "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'C', int(center)), "assetType": "OPTION"}},
        {"instruction": "BUY_TO_CLOSE",  "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'P', int(center)), "assetType": "OPTION"}},
        {"instruction": "SELL_TO_CLOSE", "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'C', int(center + cw)), "assetType": "OPTION"}},
        {"instruction": "SELL_TO_CLOSE", "quantity": qty, "instrument": {"symbol": build_schwab_symbol(expiry_yymmdd, 'P', int(center - pw)), "assetType": "OPTION"}},
    ]

    order_spec = {
        "orderType": "NET_DEBIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "complexOrderStrategyType": "CUSTOM",
        "price": f"{price:.2f}",
        "orderLegCollection": schwab_legs,
    }

    return legs, price, order_spec


_STATUS_MAP = {
    'FILLED': 'filled',
    'WORKING': 'working',
    'QUEUED': 'working',
    'ACCEPTED': 'working',
    'PENDING_ACTIVATION': 'working',
    'AWAITING_PARENT_ORDER': 'working',
    'AWAITING_CONDITION': 'working',
    'PENDING_REPLACE': 'working',
    'PENDING_CANCEL': 'working',
    'CANCELED': 'cancelled',
    'CANCELLED': 'cancelled',
    'EXPIRED': 'cancelled',
    'REPLACED': 'replaced',
    'REJECTED': 'rejected',
}


def map_broker_status(status: str) -> str:
    """Normalize broker status strings (§5.4). Unknown statuses map to 'working'."""
    norm = (status or '').strip().upper()
    return _STATUS_MAP.get(norm, 'working')
