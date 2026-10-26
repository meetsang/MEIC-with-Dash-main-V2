"""Configuration schema for Inverse MEIC (INVERSE_MEIC_SPX)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from blocks.stop.profiles.inverse_meic import INVERSE_MEIC_PROFILE
from strategies.base import StrategyConfig


@dataclass
class InverseMEICConfig(StrategyConfig):
    name: str = 'INVERSE_MEIC_SPX'
    ticker: str = 'SPX'
    broker: str = 'tastytrade'
    enabled: bool = True
    confirmed: bool = True
    paper: bool = True
    scheduled: bool = False
    stop_profile: str = INVERSE_MEIC_PROFILE
    spread_width: int = 25
    target_debit: float = 0.90
    max_debit: float = 1.10
    profit_target_multiple: float = 3.0
    premarket_gap_threshold: float = -0.005
    orb_range_threshold: float = 25.0
    vix_threshold: float = 18.0
    atr_threshold: float = 25.0
    quantity: int = 1
    force_eod_exit: bool = True
    eod_exit_time_cst: str = '14:50'
    root: str = ''
    phase_names: List[str] = field(default_factory=lambda: [
        'phase_debit_multiple_profit_target',
    ])
