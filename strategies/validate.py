"""Validate strategies.yaml and block configs at startup (G9)."""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

import blocks.stop.profiles  # noqa: F401 — register stop profiles
from blocks.entry.config import CreditEntryConfig
from blocks.stop.stop_profile import _PROFILE_REGISTRY
from common import tt_config
from strategies.loader import CONFIG_PATH, _build_config, _resolve_classes, load_strategies_yaml_raw

log = logging.getLogger(__name__)

_VALID_BROKERS = frozenset({'tastytrade', 'schwab'})
# Misconfigured optional strategies are skipped; core live paths must fail closed.
_CRITICAL_STRATEGIES = frozenset({'MEIC_IC', 'MANUAL_SPREAD'})


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

    stop_profile = str(entry.get('exit_profile') or entry.get('stop_profile', 'meic_credit_spread'))
    if stop_profile not in _PROFILE_REGISTRY:
        raise StrategyConfigError(
            f"strategy {name!r}: unknown stop_profile {stop_profile!r} "
            f"(registered: {sorted(_PROFILE_REGISTRY)})"
        )

    try:
        strat_cls, cfg_cls = _resolve_classes(module_path, entry)
    except Exception as exc:
        raise StrategyConfigError(f"strategy {name!r}: cannot load module {module_path!r}: {exc}") from exc

    overrides = entry.get('entry_config') or entry.get('config_overrides') or {}
    if overrides and not isinstance(overrides, dict):
        raise StrategyConfigError(f"strategy {name!r}: entry_config must be a mapping")

    scheduled = bool(entry.get('scheduled', True))
    if scheduled:
        try:
            CreditEntryConfig.from_overrides(overrides if isinstance(overrides, dict) else None)
        except ValueError as exc:
            raise StrategyConfigError(f"strategy {name!r}: invalid entry_config: {exc}") from exc
    else:
        # Standalone strategies: validate overrides against their own config class.
        try:
            cfg = _build_config(entry, cfg_cls)
        except ValueError as exc:
            raise StrategyConfigError(f"strategy {name!r}: invalid config_overrides: {exc}") from exc

    paper = entry.get('paper')
    use_paper = bool(paper) if paper is not None else bool(tt_config.PAPER_MODE)
    if use_paper and broker == 'tastytrade':
        # Journal paper: record fills locally using live TT market data (no tastyware).
        if not (tt_config.TT_REFRESH_TOKEN and tt_config.TT_CLIENT_SECRET):
            raise StrategyConfigError(
                f"strategy {name!r}: paper=true requires TT_REFRESH_TOKEN and "
                f"TT_CLIENT_SECRET in .env for live market data (journal mode)"
            )
    elif not use_paper and broker == 'tastytrade':
        if not (tt_config.TT_REFRESH_TOKEN and tt_config.TT_CLIENT_SECRET):
            raise StrategyConfigError(
                f"strategy {name!r}: paper=false requires TT_REFRESH_TOKEN and "
                f"TT_CLIENT_SECRET in .env for live TastyTrade"
            )

    if entry.get('enabled', True):
        cfg = _build_config(entry, cfg_cls)
        if getattr(cfg_cls, 'requires_confirmation', False) or cfg.requires_confirmation:
            if not bool(entry.get('confirmed', False)):
                raise StrategyConfigError(
                    f"strategy {name!r}: requires confirmed: true in strategies.yaml "
                    f"(safety gate for strategies that require explicit acknowledgment)"
                )

    if scheduled and entry.get('enabled', True):
        cfg = _build_config(entry, cfg_cls)
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
    skipped_errors: List[tuple[str, StrategyConfigError]] = []

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
        try:
            if is_enabled or fail_on_disabled:
                validate_strategy_entry(entry)
        except StrategyConfigError as exc:
            if name in _CRITICAL_STRATEGIES:
                raise
            if is_enabled:
                log.error('Strategy %s will not run: %s', name, exc)
                skipped_errors.append((name, exc))
                continue
            raise
        if is_enabled:
            enabled.append(entry)

    if not enabled:
        if skipped_errors:
            raise skipped_errors[0][1]
        raise StrategyConfigError('no enabled strategies in strategies.yaml')

    # Default MEIC entry config must be valid even without yaml overrides
    CreditEntryConfig.from_meic_config()
    return enabled
