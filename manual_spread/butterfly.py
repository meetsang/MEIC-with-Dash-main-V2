"""Manual 1-2-1 butterfly helpers (not iron butterfly / RIB)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from brokers.base import ComboLeg
from common.symbols import build_tastytrade_symbol

# Session-row / trade JSON side codes
SELL_CALL_BF = 'SCBF'
BUY_CALL_BF = 'BCBF'
SELL_PUT_BF = 'SPBF'
BUY_PUT_BF = 'BPBF'

BUTTERFLY_SIDES = frozenset({SELL_CALL_BF, BUY_CALL_BF, SELL_PUT_BF, BUY_PUT_BF})

STRUCTURE_TYPES: Dict[str, Dict[str, object]] = {
    'sell_call_bf': {'side': SELL_CALL_BF, 'opt': 'C', 'sell': True, 'label': 'Sell Call BF'},
    'buy_call_bf': {'side': BUY_CALL_BF, 'opt': 'C', 'sell': False, 'label': 'Buy Call BF'},
    'sell_put_bf': {'side': SELL_PUT_BF, 'opt': 'P', 'sell': True, 'label': 'Sell Put BF'},
    'buy_put_bf': {'side': BUY_PUT_BF, 'opt': 'P', 'sell': False, 'label': 'Buy Put BF'},
}


def is_butterfly_side(side: str) -> bool:
    return (side or '').upper() in BUTTERFLY_SIDES


def structure_type_for_side(side: str) -> str:
    side = (side or '').upper()
    for key, meta in STRUCTURE_TYPES.items():
        if meta['side'] == side:
            return key
    raise ValueError(f'unknown butterfly side: {side}')


def side_for_structure_type(structure_type: str) -> str:
    meta = STRUCTURE_TYPES.get((structure_type or '').lower())
    if not meta:
        raise ValueError(f'unknown structure_type: {structure_type}')
    return str(meta['side'])


def strikes_for_body(body_strike: int, wing_half: int) -> Tuple[int, int, int]:
    """Return (wing_low, body, wing_high) for symmetric wings."""
    half = int(wing_half)
    body = int(body_strike)
    return body - half, body, body + half


def net_butterfly_mid(
    wing_low_mid: float,
    body_mid: float,
    wing_high_mid: float,
    *,
    sell: bool,
) -> float:
    """Positive credit for sell fly; positive debit for buy fly."""
    if sell:
        return round(2 * body_mid - wing_low_mid - wing_high_mid, 2)
    return round(wing_low_mid + wing_high_mid - 2 * body_mid, 2)


def build_butterfly_legs(
    expiry_yymmdd: str,
    opt_type: str,
    wing_low: int,
    body: int,
    wing_high: int,
    quantity: int,
    *,
    sell: bool,
) -> List[ComboLeg]:
    """Build 1-2-1 butterfly legs (3 symbols, 4 leg rows)."""
    opt = opt_type.upper()
    qty = int(quantity)
    wl = build_tastytrade_symbol(expiry_yymmdd, opt, int(wing_low))
    b = build_tastytrade_symbol(expiry_yymmdd, opt, int(body))
    wh = build_tastytrade_symbol(expiry_yymmdd, opt, int(wing_high))
    if sell:
        return [
            ComboLeg(symbol=wl, action='SELL_TO_OPEN', quantity=qty),
            ComboLeg(symbol=b, action='BUY_TO_OPEN', quantity=qty * 2),
            ComboLeg(symbol=wh, action='SELL_TO_OPEN', quantity=qty),
        ]
    return [
        ComboLeg(symbol=wl, action='BUY_TO_OPEN', quantity=qty),
        ComboLeg(symbol=b, action='SELL_TO_OPEN', quantity=qty * 2),
        ComboLeg(symbol=wh, action='BUY_TO_OPEN', quantity=qty),
    ]


def order_price_for_butterfly(net_price: float, *, sell: bool) -> float:
    """Broker price: positive credit, negative debit."""
    magnitude = abs(float(net_price))
    return magnitude if sell else -magnitude


@dataclass
class ButterflyCandidate:
    structure_type: str
    side: str
    opt_type: str
    body_strike: int
    wing_low: int
    wing_high: int
    wing_half: int
    market_price: float
    wing_low_mid: float
    body_mid: float
    wing_high_mid: float
    distance_from_target: float = 0.0
    sell: bool = True

    def legs(self, expiry_yymmdd: str, quantity: int = 1) -> List[ComboLeg]:
        return build_butterfly_legs(
            expiry_yymmdd,
            self.opt_type,
            self.wing_low,
            self.body_strike,
            self.wing_high,
            quantity,
            sell=self.sell,
        )
