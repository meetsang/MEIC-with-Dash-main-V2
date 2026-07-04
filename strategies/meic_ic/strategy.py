"""MEIC Iron Condor strategy — thin tranche entry, stop_monitor handles close."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from strategies.base import BaseStrategy, StrategyConfig


@dataclass
class MEICConfig(StrategyConfig):
    name: str = 'MEIC_IC_SPX'
    phase_names: List[str] = field(default_factory=lambda: [
        'Phase1InitialStop',
        'Phase2NetCreditUpgrade',
        'Phase3SpxProximityClose',
    ])


class MEICStrategy(BaseStrategy):
    """
    Wraps existing MEIC tranche launcher.
    Entry: meic0dte/app_main.py (thin mode when BROKER=tastytrade)
    Close: stop_monitor/run.py
    """

    def __init__(self, config: MEICConfig = None, broker=None):
        config = config or MEICConfig()
        super().__init__(config, broker)
        self.logger = logging.getLogger(self.config.name)
        self._running = False

    def run(self) -> None:
        self._running = True
        self.logger.info('MEIC strategy delegates to run.py launcher + stop_monitor')

    def stop(self) -> None:
        self._running = False
        self.logger.info('MEIC strategy stopped')

    def get_status(self) -> Dict[str, Any]:
        return {
            'name': self.config.name,
            'running': self._running,
            'broker': self.config.broker,
            'ticker': self.config.ticker,
            'phases': self.config.phase_names,
        }
