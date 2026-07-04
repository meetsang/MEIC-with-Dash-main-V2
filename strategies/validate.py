"""Validate strategies.yaml and block configs at startup (G9)."""
from __future__ import annotations

from typing import Any, Dict, List

import blocks.stop.profiles  # noqa: F401 — register stop profiles
from blocks.entry.config import CreditEntryConfig
from blocks.stop.stop_profile import _PROFILE_REGISTRY
from strategies.loader import CONFIG_PATH, _resolve_classes, load_strategies_yaml_raw

_VALID_BROKERS = frozenset({'tastytrade', 'schwab'})


class StrategyConfigError(ValueError):
    """Raised when strategies.yaml or a strategy config is invalid."""


def _require(entry: dict, key: str, strategy_name: str) -> Any:
    val = entry.get(key)
    if val is None or val == '':
        raise StrategyConfigError(f"strategy {strategy_name!r}: missing required field {key!r}")
    return val


def validate_strategy_entry(entry: dict) -> None:
    name = _require(entry, 'name', entry.get('name', '?'))
    module_path = _require(entry, 'module', name)

    broker = str(entry.get('broker', 'tastytrade')).lower()
    if broker not in _VALID_BROKERS:
        raise StrategyConfigError(
            f"strategy {name!r}: broker must be one of {sorted(_VALID_BROKERS)}, got {broker!r}"
        )

    stop_profile = str(entry.get('stop_profile', 'meic_credit_spread'))
    if stop_profile not in _PROFILE_REGISTRY:
        raise StrategyConfigError(
            f"strategy {name!r}: unknown stop_profile {stop_profile!r} "
            f"(registered: {sorted(_PROFILE_REGISTRY)})"
        )

    try:
        _resolve_classes(module_path, entry)
    except Exception as exc:
        raise StrategyConfigError(f"strategy {name!r}: cannot load module {module_path!r}: {exc}") from exc

    overrides = entry.get('entry_config') or entry.get('config_overrides') or {}
    if overrides and not isinstance(overrides, dict):
        raise StrategyConfigError(f"strategy {name!r}: entry_config must be a mapping")

    try:
        CreditEntryConfig.from_overrides(overrides if isinstance(overrides, dict) else None)
    except ValueError as exc:
        raise StrategyConfigError(f"strategy {name!r}: invalid entry_config: {exc}") from exc

    scheduled = bool(entry.get('scheduled', True))
    if scheduled and entry.get('enabled', True):
        strat_cls, cfg_cls = _resolve_classes(module_path, entry)
        cfg = cfg_cls(
            name=name,
            broker=broker,
            ticker=entry.get('instrument', entry.get('ticker', 'SPX')),
            enabled=True,
            scheduled=True,
        )
        strategy = strat_cls(cfg)
        schedule_fn = getattr(strategy, 'schedule', None)
        if callable(schedule_fn) and not schedule_fn():
            raise StrategyConfigError(
                f"strategy {name!r}: scheduled=true but schedule() returned no slots"
            )


def validate_startup_config(*, fail_on_disabled: bool = False) -> List[dict]:
    """Validate strategies.yaml; return enabled strategy entries. Raises on error."""
    entries = load_strategies_yaml_raw()
    if not entries:
        raise StrategyConfigError(f'no strategies defined in {CONFIG_PATH}')

    seen_names: set[str] = set()
    enabled: List[dict] = []

    for entry in entries:
        if not isinstance(entry, dict):
            raise StrategyConfigError('each strategy entry must be a mapping')
        name = str(entry.get('name', '')).strip()
        if not name:
            raise StrategyConfigError('strategy entry missing name')
        if name in seen_names:
            raise StrategyConfigError(f'duplicate strategy name: {name!r}')
        seen_names.add(name)

        is_enabled = bool(entry.get('enabled', True))
        if is_enabled or fail_on_disabled:
            validate_strategy_entry(entry)
        if is_enabled:
            enabled.append(entry)

    if not enabled:
        raise StrategyConfigError('no enabled strategies in strategies.yaml')

    # Default MEIC entry config must be valid even without yaml overrides
    CreditEntryConfig.from_meic_config()
    return enabled
