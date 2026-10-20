"""Strategy configuration and base class."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields
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
    confirmed: bool = False
    stop_profile: str = 'meic_credit_spread'
    data_base_dir: str = 'Data'
    phase_names: List[str] = field(default_factory=list)
    # Set True on paper-only strategies that need an explicit confirmed: true in yaml.
    requires_confirmation: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


import logging

log = logging.getLogger(__name__)


def apply_config_overrides(cfg: StrategyConfig, overrides: dict | None) -> None:
    """Apply yaml config_overrides onto a StrategyConfig instance in-place."""
    if not overrides:
        return
    allowed = {f.name for f in fields(cfg)}
    unknown = set(overrides) - allowed
    if unknown:
        try:
            import importlib, sys
            mod = sys.modules.get(cfg.__class__.__module__)
            if mod:
                importlib.reload(mod)
                allowed = {f.name for f in fields(cfg)}
                unknown = set(overrides) - allowed
        except Exception:
            pass
        if getattr(cfg, 'name', '') == 'DRMA_HM_SPX':
            unknown = unknown - {'dir_paper', 'non_dir_paper', 'futures_paper'}
    if unknown:
        log.warning(
            "Strategy %s config_overrides contains fields not on %s: %s (attaching dynamically)",
            getattr(cfg, 'name', 'UNKNOWN'),
            cfg.__class__.__name__,
            sorted(unknown),
        )
    for key, value in overrides.items():
        setattr(cfg, key, value)


class BaseStrategy(ABC):
    def __init__(self, config: StrategyConfig, broker=None):
        self.config = config
        self.broker = broker

    def schedule(self) -> List[Any]:
        return []

    @abstractmethod
    def run(self) -> None:
        ...

    @abstractmethod
    def stop(self) -> None:
        ...

    @abstractmethod
    def get_status(self) -> Dict[str, Any]:
        ...
