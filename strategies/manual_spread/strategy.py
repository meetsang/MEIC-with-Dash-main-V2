"""Manual spread strategy — dashboard-triggered credit verticals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from orchestrator.scheduler import TrancheSlot
from strategies.base import BaseStrategy, StrategyConfig


def manual_stop_profile():
    import blocks.stop.profiles  # noqa: F401
    from blocks.stop.profiles.meic import meic_stop_profile as _factory
    return _factory()


def manual_entry_block(broker, log=None):
    from blocks.entry.config import CreditEntryConfig
    from blocks.entry.credit_spread import CreditSpreadEntry
    from manual_spread import config as ms_config

    cfg = CreditEntryConfig(
        otm_min=ms_config.OTM_MIN,
        otm_max=ms_config.OTM_MAX,
        min_market_credit=ms_config.MIN_MARKET_CREDIT,
        quote_source='api',
    )
    return CreditSpreadEntry(broker, cfg, log=log)


@dataclass
class ManualSpreadConfig(StrategyConfig):
    name: str = 'MANUAL_SPREAD'
    scheduled: bool = False


class ManualSpreadStrategy(BaseStrategy):
    """Not scheduled; entry via dashboard / manual_spread/entry.py."""

    def __init__(self, config: Optional[ManualSpreadConfig] = None, broker=None):
        config = config or ManualSpreadConfig()
        super().__init__(config, broker)

    def schedule(self) -> List[TrancheSlot]:
        return []

    def stop_profile(self):
        return manual_stop_profile()

    def entry_block(self, broker, log=None):
        return manual_entry_block(broker, log=log)

    def run(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def get_status(self) -> Dict[str, Any]:
        return {
            'name': self.config.name,
            'scheduled': False,
            'stop_profile': self.stop_profile().name,
        }
