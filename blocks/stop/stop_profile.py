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
    profit_target_short: Optional[float] = None
    use_oco_short_stop: bool = False
    adjust_to_breakeven_on_companion_breach: bool = False
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
    import blocks.stop.profiles  # noqa: F401
    from blocks.stop.profiles.meic import MEIC_CREDIT_SPREAD_PROFILE, meic_stop_profile

    key = state.get('stop_profile')
    if not key:
        strategy = state.get('entry', {}).get('strategy') or state.get('strategy')
        if strategy == 'MANUAL_SPREAD':
            from strategies.manual_spread.strategy import manual_stop_profile
            return manual_stop_profile()
        key = MEIC_CREDIT_SPREAD_PROFILE

    factory = _PROFILE_REGISTRY.get(str(key))
    if factory is None:
        return meic_stop_profile()
    return factory()
