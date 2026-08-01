"""Daily dashboard overrides for standalone strategies."""
from __future__ import annotations

import json
import os
from dataclasses import fields
from datetime import date
from typing import Any, Dict, Optional, Type

from common.trades_layout import active_dir_for_strategy
from strategies.base import StrategyConfig, apply_config_overrides

# Permanent-only fields — never overridable from dashboard for a day.
NON_OVERRIDABLE = frozenset({
    'name', 'confirmed', 'requires_confirmation', 'broker', 'root',
})


def session_state_path(strategy_name: str, root: str, for_date: Optional[date] = None) -> str:
    d = (for_date or date.today()).isoformat()
    active = active_dir_for_strategy(strategy_name, root)
    os.makedirs(active, exist_ok=True)
    return os.path.join(active, f'session_{d}.json')


def load_session_state(strategy_name: str, root: str) -> Dict[str, Any]:
    path = session_state_path(strategy_name, root)
    if not os.path.isfile(path):
        return {'entered': False, 'skip_reason': None, 'overrides': {}}
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {'entered': False, 'skip_reason': None, 'overrides': {}}
    if 'overrides' not in state:
        state['overrides'] = {}
    return state


def save_session_state(strategy_name: str, root: str, state: Dict[str, Any]) -> None:
    path = session_state_path(strategy_name, root)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2)
        f.flush()
    os.replace(tmp, path)


def get_overrides(strategy_name: str, root: str) -> Dict[str, Any]:
    return dict(load_session_state(strategy_name, root).get('overrides') or {})


def set_overrides(strategy_name: str, root: str, updates: Dict[str, Any], cfg_cls: Type[StrategyConfig]) -> Dict[str, Any]:
    allowed = {f.name for f in fields(cfg_cls)} - NON_OVERRIDABLE
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f'unknown override keys: {sorted(unknown)}')
    state = load_session_state(strategy_name, root)
    overrides = dict(state.get('overrides') or {})
    overrides.update(updates)
    state['overrides'] = overrides
    save_session_state(strategy_name, root, state)
    return overrides


def delete_override(strategy_name: str, root: str, key: str) -> Dict[str, Any]:
    state = load_session_state(strategy_name, root)
    overrides = dict(state.get('overrides') or {})
    overrides.pop(key, None)
    state['overrides'] = overrides
    save_session_state(strategy_name, root, state)
    return overrides


def effective_config(cfg: StrategyConfig, strategy_name: str, root: str) -> StrategyConfig:
    """Return a copy of cfg with today's overrides applied."""
    import copy
    merged = copy.copy(cfg)
    overrides = get_overrides(strategy_name, root)
    if overrides:
        apply_config_overrides(merged, overrides)
    return merged


def is_disabled_for_today(cfg: StrategyConfig, strategy_name: str, root: str) -> bool:
    overrides = get_overrides(strategy_name, root)
    if 'enabled' in overrides:
        return not bool(overrides['enabled'])
    return not cfg.enabled
