"""MEIC strategy runtime settings — 15c OCO flag, aggressive side skew, and side toggles."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple

from common import trades_layout

log = logging.getLogger(__name__)

SETTINGS_FILENAME = 'meic_settings.json'

FIRST_FOUR_LOTS = ('10-30', '11-00', '12-00', '01-00')
LAST_TWO_LOTS = ('01-15', '02-15')

BASE_CREDIT_FIRST_FOUR = (1.20, 1.60)
BASE_CREDIT_LAST_TWO = (0.50, 1.00)


@dataclass
class MeicSettings:
    use_oco_15c: bool = False
    aggressive_side: str = 'P'  # 'P', 'C', or 'NONE'
    aggressive_pct: float = 0.25
    side_ccs_enabled: bool = True
    side_pcs_enabled: bool = True
    paper_mode: bool = True
    companion_breach_tightening: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'MeicSettings':
        valid_keys = {
            'use_oco_15c',
            'aggressive_side',
            'aggressive_pct',
            'side_ccs_enabled',
            'side_pcs_enabled',
            'paper_mode',
            'companion_breach_tightening',
        }
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


def meic_settings_path(root: Optional[str] = None) -> str:
    if root:
        norm = os.path.normpath(root)
        if os.path.basename(norm) == 'trades':
            return os.path.join(norm, SETTINGS_FILENAME)
        if os.path.isdir(os.path.join(norm, 'trades')):
            return os.path.join(norm, 'trades', SETTINGS_FILENAME)
    return trades_layout.ops_path(SETTINGS_FILENAME, root=root)


def get_meic_settings(root: Optional[str] = None) -> MeicSettings:
    path = meic_settings_path(root)
    file_settings: Optional[MeicSettings] = None
    if os.path.isfile(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            file_settings = MeicSettings.from_dict(data)
        except Exception as exc:
            log.warning('Could not read %s: %s — using defaults', path, exc)

    # Environment variables (override if explicitly provided, fallback to file or default)
    env_oco = os.environ.get('MEIC_15C_OCO_ENABLED')
    if env_oco is not None:
        use_oco = env_oco.lower() in ('1', 'true', 'yes')
    elif file_settings is not None:
        use_oco = file_settings.use_oco_15c
    else:
        use_oco = False

    env_agg_side = os.environ.get('MEIC_AGGRESSIVE_SIDE')
    if env_agg_side is not None:
        agg_side_val = env_agg_side.strip().upper()
        aggressive_side = agg_side_val if agg_side_val in ('P', 'C', 'NONE') else 'P'
    elif file_settings is not None:
        aggressive_side = file_settings.aggressive_side
    else:
        aggressive_side = 'P'

    env_agg_pct = os.environ.get('MEIC_AGGRESSIVE_PCT')
    if env_agg_pct is not None:
        try:
            aggressive_pct = float(env_agg_pct)
        except ValueError:
            aggressive_pct = 0.25
    elif file_settings is not None:
        aggressive_pct = file_settings.aggressive_pct
    else:
        aggressive_pct = 0.25

    env_ccs = os.environ.get('MEIC_CCS_ENABLED')
    if env_ccs is not None:
        side_ccs = env_ccs.lower() in ('1', 'true', 'yes')
    elif file_settings is not None:
        side_ccs = file_settings.side_ccs_enabled
    else:
        side_ccs = True

    env_pcs = os.environ.get('MEIC_PCS_ENABLED')
    if env_pcs is not None:
        side_pcs = env_pcs.lower() in ('1', 'true', 'yes')
    elif file_settings is not None:
        side_pcs = file_settings.side_pcs_enabled
    else:
        side_pcs = True

    env_meic_paper = os.environ.get('MEIC_PAPER_MODE')
    if env_meic_paper is not None:
        paper_mode = env_meic_paper.lower() in ('1', 'true', 'yes')
    elif file_settings is not None and getattr(file_settings, 'paper_mode', None) is not None:
        paper_mode = file_settings.paper_mode
    elif os.environ.get('PAPER_MODE') is not None:
        paper_mode = os.environ.get('PAPER_MODE').lower() in ('1', 'true', 'yes')
    else:
        paper_mode = True

    env_companion = os.environ.get('MEIC_COMPANION_BREACH_TIGHTENING')
    if env_companion is not None:
        companion_tighten = env_companion.lower() in ('1', 'true', 'yes')
    elif file_settings is not None and getattr(file_settings, 'companion_breach_tightening', None) is not None:
        companion_tighten = bool(file_settings.companion_breach_tightening)
    else:
        companion_tighten = False

    return MeicSettings(
        use_oco_15c=use_oco,
        aggressive_side=aggressive_side,
        aggressive_pct=aggressive_pct,
        side_ccs_enabled=side_ccs,
        side_pcs_enabled=side_pcs,
        paper_mode=paper_mode,
        companion_breach_tightening=companion_tighten,
    )


def save_meic_settings(settings: MeicSettings, root: Optional[str] = None) -> str:
    path = meic_settings_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(settings.to_dict(), f, indent=2)
        f.flush()
    os.replace(tmp, path)
    log.info('Saved MEIC settings to %s: %s', path, settings)
    return path


def update_meic_settings(root: Optional[str] = None, **kwargs) -> MeicSettings:
    current = get_meic_settings(root=root)
    d = current.to_dict()
    for k, v in kwargs.items():
        if hasattr(current, k):
            d[k] = v
    updated = MeicSettings.from_dict(d)
    save_meic_settings(updated, root=root)
    return updated


def is_15c_oco_enabled(root: Optional[str] = None) -> bool:
    return get_meic_settings(root=root).use_oco_15c


def is_meic_paper_mode(root: Optional[str] = None) -> bool:
    return get_meic_settings(root=root).paper_mode


def is_companion_tightening_enabled(root: Optional[str] = None) -> bool:
    """Return whether companion breach tightening to breakeven is enabled (normally False)."""
    return get_meic_settings(root=root).companion_breach_tightening


def is_meic_side_enabled(side: str, root: Optional[str] = None) -> bool:
    s = side.upper()
    settings = get_meic_settings(root=root)
    if s == 'C':
        return settings.side_ccs_enabled
    if s == 'P':
        return settings.side_pcs_enabled
    return True


def get_meic_tranche_credits(
    lot: str,
    side: str,
    settings: Optional[MeicSettings] = None,
    root: Optional[str] = None,
) -> Tuple[float, float]:
    """Calculate (credit_min, credit_max) for a given lot and side under current settings."""
    cfg = settings or get_meic_settings(root=root)
    lot_clean = lot.strip()
    side_clean = side.strip().upper()

    if lot_clean in FIRST_FOUR_LOTS:
        base_min, base_max = BASE_CREDIT_FIRST_FOUR
    elif lot_clean in LAST_TWO_LOTS:
        base_min, base_max = BASE_CREDIT_LAST_TWO
    else:
        # Fallback if an unexpected lot name is given
        base_min, base_max = BASE_CREDIT_FIRST_FOUR

    if cfg.aggressive_side.upper() == side_clean:
        multiplier = 1.0 + cfg.aggressive_pct
        return round(base_min * multiplier, 2), round(base_max * multiplier, 2)

    return base_min, base_max


def reconcile_meic_active_stops(broker, desired_oco: bool, root: Optional[str] = None) -> List[str]:
    """Switch active open MEIC trade orders between STOP_LIMIT and OCO to match desired_oco."""
    import glob
    from blocks.stop import state as state_mod

    base_root = root or trades_layout.project_root()
    norm = os.path.normpath(base_root)
    if os.path.basename(norm) == 'trades':
        active_dir = os.path.join(norm, 'active', trades_layout.STRATEGY_MEIC)
    else:
        active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_MEIC, root=base_root)
    if not os.path.isdir(active_dir):
        return []

    modified: List[str] = []
    pattern = os.path.join(active_dir, '*.json')
    for path in glob.glob(pattern):
        try:
            state = state_mod.load_state(path)
            if state.get('status') != 'open':
                continue
            active = state.get('active_stop') or {}
            old_oid = active.get('order_id')
            if not old_oid or active.get('status') != 'working':
                continue

            current_is_oco = (active.get('type') == 'OCO')
            if current_is_oco == desired_oco:
                continue

            short = state.get('short_leg') or {}
            short_sym = short.get('symbol')
            if not short_sym:
                continue

            qty = int(active.get('quantity') or state.get('stop_quantity') or 1)
            stop_price = float(active.get('stop_price') or 0.0)
            limit_price = float(active.get('limit_price') or 0.0)
            phase = active.get('phase', 1)
            if stop_price <= 0:
                continue

            # Cancel old order
            log.info(
                'Reconciling stop mode for %s: cancelling order %s (%s -> %s)',
                path,
                old_oid,
                'OCO' if current_is_oco else 'STOP_LIMIT',
                'OCO' if desired_oco else 'STOP_LIMIT',
            )
            broker.cancel_order(str(old_oid))

            if desired_oco:
                res = broker.place_oco_order(
                    short_sym,
                    qty,
                    profit_price=0.15,
                    stop_price=stop_price,
                    limit_price=limit_price,
                )
                new_type = 'OCO'
                pt_price = 0.15
            else:
                res = broker.place_stop_order(
                    short_sym,
                    qty,
                    stop_price=stop_price,
                    limit_price=limit_price,
                )
                new_type = 'STOP_LIMIT'
                pt_price = None

            if res.success:
                new_oid = str(res.order_id)
                state['active_stop'] = {
                    'order_id': new_oid,
                    'type': new_type,
                    'stop_price': stop_price,
                    'limit_price': limit_price,
                    'profit_price': pt_price,
                    'phase': phase,
                    'status': 'working',
                    'placed_at': state_mod.now_iso(),
                    'quantity': qty,
                }
                state_mod.append_stop_history(
                    state,
                    action='replaced',
                    order_id=new_oid,
                    price=stop_price,
                    phase=phase,
                    reason='switch_oco_flag_enabled' if desired_oco else 'switch_oco_flag_disabled',
                )
                state_mod.save_state(path, state)
                modified.append(path)
                log.info('Replaced stop on %s with %s (#%s)', path, new_type, new_oid)
            else:
                log.error('Failed to place replacement order for %s: %s', path, res.message)
        except Exception as exc:
            log.warning('Error reconciling stop mode for %s: %s', path, exc)

    return modified
