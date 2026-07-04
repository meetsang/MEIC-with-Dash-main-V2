"""MEIC scheduled tranche strategy (V2 registry entry)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, Dict, List, Optional

from orchestrator.scheduler import TrancheSlot
from strategies.base import BaseStrategy, StrategyConfig


def meic_stop_profile():
    import blocks.stop.profiles  # noqa: F401
    from blocks.stop.profiles.meic import meic_stop_profile as _factory
    return _factory()


def meic_entry_block(broker, log=None):
    from blocks.entry.credit_spread import CreditSpreadEntry
    from blocks.entry.config import CreditEntryConfig

    return CreditSpreadEntry(broker, CreditEntryConfig.from_meic_config(), log=log)


MEIC_TRANCHE_SLOTS = [
    TrancheSlot('11-00', time(10, 59), time(11, 5), 'MEIC_IC'),
    TrancheSlot('12-00', time(11, 59), time(12, 5), 'MEIC_IC'),
    TrancheSlot('12-30', time(12, 29), time(12, 35), 'MEIC_IC'),
    TrancheSlot('01-15', time(13, 14), time(13, 20), 'MEIC_IC'),
    TrancheSlot('01-45', time(13, 44), time(13, 50), 'MEIC_IC'),
    TrancheSlot('02-00', time(13, 59), time(14, 5), 'MEIC_IC'),
]


@dataclass
class MEICConfig(StrategyConfig):
    name: str = 'MEIC_IC'
    scheduled: bool = True
    phase_names: List[str] = field(default_factory=lambda: [
        'phase1_initial_stop',
        'phase2_net_credit_upgrade',
        'phase3_spx_proximity',
    ])


class MEICStrategy(BaseStrategy):
    """Scheduled MEIC iron condor — entry via session CSV + entry monitor."""

    def __init__(self, config: Optional[MEICConfig] = None, broker=None):
        config = config or MEICConfig()
        super().__init__(config, broker)

    def schedule(self) -> List[TrancheSlot]:
        if not getattr(self.config, 'scheduled', True):
            return []
        return list(MEIC_TRANCHE_SLOTS)

    def pre_entry_check(self) -> bool:
        return True

    def stop_profile(self):
        return meic_stop_profile()

    def entry_block(self, broker, log=None):
        return meic_entry_block(broker, log=log)

    def run(self) -> None:
        raise NotImplementedError('MEIC entry runs via EntryMonitorRunner on session CSV rows')

    def stop(self) -> None:
        pass

    def get_status(self) -> Dict[str, Any]:
        return {
            'name': self.config.name,
            'instrument': self.config.ticker,
            'broker': self.config.broker,
            'scheduled': self.config.scheduled,
            'slots': [s.lot for s in self.schedule()],
            'phases': self.config.phase_names,
            'stop_profile': self.stop_profile().name,
        }
