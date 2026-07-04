"""Risk management utilities (ported from spx-bot)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskLimits:
    max_daily_loss: float = 4000.0
    max_individual_position_loss: float = 400.0


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_daily_loss_limit(self, total_pnl: float) -> bool:
        return total_pnl > -self.limits.max_daily_loss

    def check_position_loss(self, pnl: float) -> bool:
        return pnl > -self.limits.max_individual_position_loss
