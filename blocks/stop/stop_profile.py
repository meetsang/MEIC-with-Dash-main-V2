"""Strategy-agnostic stop configuration for the Stop block."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from blocks.stop.phases import PhaseBase


@dataclass
class StopProfile:
    """Named stop behavior bundle supplied by a strategy."""

    name: str
    spread_type: str  # 'credit' | 'debit' (debit deferred)
    phases: List['PhaseBase']
    long_close_delay_sec: int = 30
    breach_calc: Callable[[float, float], float] | None = None
    breach_condition: Callable[[float, float], bool] | None = None

    def __post_init__(self) -> None:
        if self.breach_calc is None or self.breach_condition is None:
            from blocks.stop.breach import spread_breach_triggered, spread_mark_price

            self.breach_calc = self.breach_calc or spread_mark_price
            self.breach_condition = self.breach_condition or spread_breach_triggered


_PROFILE_REGISTRY: Dict[str, Callable[[], StopProfile]] = {}


def register_stop_profile(name: str, factory: Callable[[], StopProfile]) -> None:
    _PROFILE_REGISTRY[name] = factory


def resolve_stop_profile(state: Dict[str, Any]) -> StopProfile:
    from blocks.stop.profiles.meic import MEIC_CREDIT_SPREAD_PROFILE, meic_stop_profile

    key = str(state.get('stop_profile') or MEIC_CREDIT_SPREAD_PROFILE)
    factory = _PROFILE_REGISTRY.get(key)
    if factory is None:
        return meic_stop_profile()
    return factory()
