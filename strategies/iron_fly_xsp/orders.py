"""Order construction and status normalization for XSP Iron Fly."""
from __future__ import annotations

import math
from typing import List

from brokers.base import ComboLeg


def build_xsp_symbol(expiry_yymmdd: str, opt_type: str, strike: int) -> str:
    """Format standard XSP option symbol: .XSP261003C575."""
    ot = opt_type.upper().strip()
    return f'.XSP{expiry_yymmdd}{ot}{strike}'


def round_credit_down(credit: float, tick: float = 0.05) -> float:
    """Round Net Credit down to nearest combo tick (conservative sell)."""
    return round(math.floor(round(credit, 4) / tick) * tick, 2)


def round_debit_up(debit: float, tick: float = 0.05) -> float:
    """Round Net Debit up to nearest combo tick (conservative buy)."""
    return round(math.ceil(round(debit, 4) / tick) * tick, 2)


def build_fly_legs_xsp(
    center: int,
    cw: int,
    pw: int,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """
    Constructs the 4 legs for opening an XSP Iron Fly (Net Credit).
      - SELL Call @ center
      - SELL Put  @ center
      - BUY  Call @ center + cw
      - BUY  Put  @ center - pw
    """
    return [
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'C', center),
            quantity=quantity,
            action='SELL_TO_OPEN',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'P', center),
            quantity=quantity,
            action='SELL_TO_OPEN',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'C', center + cw),
            quantity=quantity,
            action='BUY_TO_OPEN',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'P', center - pw),
            quantity=quantity,
            action='BUY_TO_OPEN',
        ),
    ]


def build_closing_legs_xsp(
    center: int,
    cw: int,
    pw: int,
    expiry_yymmdd: str,
    quantity: int = 1,
) -> List[ComboLeg]:
    """
    Constructs the 4 legs for closing an XSP Iron Fly (Net Debit).
      - BUY  Call @ center
      - BUY  Put  @ center
      - SELL Call @ center + cw
      - SELL Put  @ center - pw
    """
    return [
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'C', center),
            quantity=quantity,
            action='BUY_TO_CLOSE',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'P', center),
            quantity=quantity,
            action='BUY_TO_CLOSE',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'C', center + cw),
            quantity=quantity,
            action='SELL_TO_CLOSE',
        ),
        ComboLeg(
            symbol=build_xsp_symbol(expiry_yymmdd, 'P', center - pw),
            quantity=quantity,
            action='SELL_TO_CLOSE',
        ),
    ]


def normalize_broker_status(broker_status: str) -> str:
    """Normalize broker order status string."""
    raw = str(broker_status or '').upper().strip()
    if raw in ('FILLED', 'FILL'):
        return 'FILLED'
    if raw in ('WORKING', 'OPEN', 'PENDING', 'SUBMITTED', 'QUEUED'):
        return 'WORKING'
    if raw in ('CANCELLED', 'CANCELED', 'EXPIRED'):
        return 'CANCELLED'
    if raw in ('REJECTED', 'FAILED'):
        return 'REJECTED'
    return 'UNKNOWN'
