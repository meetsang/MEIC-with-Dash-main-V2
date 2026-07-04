"""Strategy configuration and base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class StrategyConfig:
    name: str
    broker: str = 'tastytrade'
    ticker: str = 'SPX'
    enabled: bool = True
    scheduled: bool = True
    dry_run: bool = False
    paper: bool = False
    data_base_dir: str = 'Data'
    phase_names: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


class BaseStrategy(ABC):
    def __init__(self, config: StrategyConfig, broker=None):
        self.config = config
        self.broker = broker

    @abstractmethod
    def run(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        ...
