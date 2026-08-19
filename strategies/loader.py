"""Load strategy definitions from YAML config."""
from __future__ import annotations

import importlib
import logging
import os
from typing import List, Tuple, Type

import yaml

from common import tt_config
from strategies.base import BaseStrategy, StrategyConfig, apply_config_overrides

log = logging.getLogger(__name__)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CONFIG_PATH = os.path.join(ROOT, 'config', 'strategies.yaml')

_MODULE_DEFAULTS = {
    'strategies.meic.strategy': ('MEICStrategy', 'MEICConfig'),
    'strategies.manual_spread.strategy': ('ManualSpreadStrategy', 'ManualSpreadConfig'),
    'strategies.meic_ic.strategy': ('MEICStrategy', 'MEICConfig'),
    'strategies.iron_fly.strategy': ('IronFlyStrategy', 'IronFlyConfig'),
    'strategies.skyline.strategy': ('SkylineStrategy', 'SkylineConfig'),
    'strategies.perma_bull_pcs.strategy': ('PermaBullPCSStrategy', 'PermaBullPCSConfig'),
}


def load_strategies_yaml_raw() -> list:
    if not os.path.exists(CONFIG_PATH):
        return []
    with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    return list(data.get('strategies', []))


def _resolve_classes(module_path: str, entry: dict) -> Tuple[Type[BaseStrategy], Type[StrategyConfig]]:
    class_name = entry.get('class')
    config_name = entry.get('config_class')
    if class_name and config_name:
        mod = importlib.import_module(module_path)
        return getattr(mod, class_name), getattr(mod, config_name)
    defaults = _MODULE_DEFAULTS.get(module_path)
    if defaults:
        mod = importlib.import_module(module_path)
        return getattr(mod, defaults[0]), getattr(mod, defaults[1])
    mod = importlib.import_module(module_path)
    # Last resort: Strategy + Config suffix from module basename
    base = module_path.rsplit('.', 1)[-1]
    return getattr(mod, f'{base.title()}Strategy'), getattr(mod, f'{base.title()}Config')


def _build_config(entry: dict, cfg_cls: Type[StrategyConfig]) -> StrategyConfig:
    name = entry.get('name', cfg_cls.name if hasattr(cfg_cls, 'name') else 'UNKNOWN')
    overrides = entry.get('entry_config') or entry.get('config_overrides') or {}

    paper = entry.get('paper')
    if paper is None:
        paper = tt_config.PAPER_MODE

    cfg = cfg_cls(
        name=name,
        broker=entry.get('broker', 'tastytrade'),
        ticker=entry.get('instrument', entry.get('ticker', 'SPX')),
        enabled=bool(entry.get('enabled', True)),
        scheduled=bool(entry.get('scheduled', True)),
        paper=bool(paper),
        confirmed=bool(entry.get('confirmed', False)),
        stop_profile=str(entry.get('exit_profile') or entry.get('stop_profile', 'meic_credit_spread')),
    )
    apply_config_overrides(cfg, overrides if isinstance(overrides, dict) else None)
    return cfg


def load_strategies(*, include_disabled: bool = False) -> List[Tuple[BaseStrategy, StrategyConfig]]:
    from strategies.validate import validate_startup_config

    valid_enabled = validate_startup_config(fail_on_disabled=include_disabled)
    valid_names = {e['name'] for e in valid_enabled}
    entries = load_strategies_yaml_raw()

    result: List[Tuple[BaseStrategy, StrategyConfig]] = []
    for entry in entries:
        name = entry.get('name', '')
        is_enabled = bool(entry.get('enabled', True))
        if not is_enabled and not include_disabled:
            continue
        if is_enabled and name not in valid_names:
            continue
        module_path = entry.get('module', '')
        if not module_path:
            log.warning('Strategy entry missing module: %s', entry)
            continue
        try:
            strat_cls, cfg_cls = _resolve_classes(module_path, entry)
        except Exception as exc:
            log.error('Failed to load strategy module %s: %s', module_path, exc)
            continue
        cfg = _build_config(entry, cfg_cls)
        result.append((strat_cls(cfg), cfg))
    return result


def load_enabled_strategies() -> List[BaseStrategy]:
    """Return instantiated enabled strategies (scheduled + manual + standalone)."""
    return [strat for strat, _cfg in load_strategies()]


def load_standalone_strategies() -> List[Tuple[BaseStrategy, StrategyConfig]]:
    """Return enabled strategies that are not session-CSV tranche strategies."""
    return [
        (strat, cfg) for strat, cfg in load_strategies()
        if not cfg.scheduled
    ]
