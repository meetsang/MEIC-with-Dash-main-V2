"""Pure functions for building Skyline butterfly legs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from brokers.base import ComboLeg
from common.symbols import build_tastytrade_symbol


@dataclass
class ButterflyConfig:
    name: str
    type: str  # 'call' | 'put'
    wing1: int
    body: int
    wing2: int


def round_atm(spot: float, rounding: int = 5) -> int:
    return int(round(spot / rounding) * rounding)


def build_butterfly_legs(
    atm: int,
    expiry_yymmdd: str,
    butterfly: ButterflyConfig,
    quantity: int = 1,
) -> List[ComboLeg]:
    """Build 4 legs for one butterfly from ATM-relative offsets."""
    opt_type = 'C' if butterfly.type.lower() == 'call' else 'P'
    strikes = [atm + butterfly.wing1, atm + butterfly.body, atm + butterfly.wing2]
    symbols = [build_tastytrade_symbol(expiry_yymmdd, opt_type, s) for s in strikes]
    return [
        ComboLeg(symbol=symbols[0], action='BUY_TO_OPEN', quantity=quantity),
        ComboLeg(symbol=symbols[1], action='SELL_TO_OPEN', quantity=quantity * 2),
        ComboLeg(symbol=symbols[2], action='BUY_TO_OPEN', quantity=quantity),
    ]
