"""Manual place-order plan fields — session CSV + trade JSON."""
from __future__ import annotations

from typing import Any, Dict

from blocks.session.plan import SessionRow
from blocks.stop.stop_math import sl_pct_from_stop_multiplier, stop_multiplier_from_sl_pct
from manual_spread import config as ms_config

DEBIT_STRUCTURES = frozenset({'debit_p', 'debit_c', 'pds', 'cds'})
DEBIT_SIDES = frozenset({'PDS', 'CDS'})


def is_debit_side_or_structure(
    side: str = '',
    structure_type: str = '',
    spread_type: str = '',
) -> bool:
    if (spread_type or '').strip().lower() == 'debit':
        return True
    if (structure_type or '').strip().lower() in DEBIT_STRUCTURES:
        return True
    if (side or '').strip().upper() in DEBIT_SIDES:
        return True
    return False


def normalize_pct_ratio(val: Any, default: float = 0.50) -> float:
    """Normalize a percentage input into a ratio (e.g. 0.50 = 50%, 1.50 = 150%, 0.335 = 33.5%).

    Dashboard JS divides percentage inputs by 100 before sending (so 50% -> 0.50,
    150% -> 1.50, 200% -> 2.00). Raw callers that pass percentage numbers > 10.0
    (e.g. 50, 100, 150, 200) are divided by 100.0 once.
    """
    if val is None or str(val).strip() == '':
        return default
    f = float(val)
    if f > 10.0:
        f = f / 100.0
    return round(max(0.0, f), 6)


def plan_fields_from_request(data: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize dashboard Place Order payload into SessionRow kwargs."""
    on_unfilled = str(data.get('on_unfilled') or 'none').strip() or 'none'
    chase = on_unfilled == 'chase_same_trade'
    chase_max = int(data.get('chase_max_attempts') or data.get('chase1_max') or 0) if chase else 0
    chase_floor = float(
        data.get('chase_floor') or data.get('credit_min') or 0,
    ) if chase else 0.0
    fill_wait = int(data.get('fill_wait_sec') or ms_config.OPEN_FILL_POLL_SEC)
    max_attempts = int(data.get('max_attempts') or (1 + chase_max if chase else 1))

    is_debit = is_debit_side_or_structure(
        side=str(data.get('side') or ''),
        structure_type=str(data.get('structure_type') or ''),
        spread_type=str(data.get('spread_type') or ''),
    )
    spread_type = 'debit' if is_debit else 'credit'

    raw_pt = data.get('profit_target_pct')
    default_pt = 1.00 if is_debit else 0.50
    if raw_pt is None or str(raw_pt).strip() == '':
        pt_pct = default_pt
    else:
        pt_pct = normalize_pct_ratio(raw_pt, default=default_pt)

    raw_sl = data.get('stop_loss_pct')
    raw_mult = data.get('stop_multiplier')
    if raw_sl is not None and str(raw_sl).strip() != '':
        sl_pct = normalize_pct_ratio(raw_sl, default=0.50 if is_debit else 1.0)
        stop_mult = stop_multiplier_from_sl_pct(sl_pct, spread_type=spread_type)
    elif raw_mult is not None and str(raw_mult).strip() != '':
        val = float(raw_mult)
        if val > 10.0:
            sl_pct = normalize_pct_ratio(val, default=0.50 if is_debit else 1.0)
            stop_mult = stop_multiplier_from_sl_pct(sl_pct, spread_type=spread_type)
        else:
            stop_mult = val
            sl_pct = sl_pct_from_stop_multiplier(stop_mult, spread_type=spread_type)
    else:
        sl_pct = 0.50 if is_debit else 1.0
        stop_mult = stop_multiplier_from_sl_pct(sl_pct, spread_type=spread_type)

    return {
        'stop_multiplier': stop_mult,
        'stop_loss_pct': sl_pct,
        'profit_target_pct': pt_pct,
        'width': str(data.get('width') or ''),
        'credit_min': chase_floor,
        'credit_max': 0.0,
        'chase1_mode': 'chase_same_trade' if chase else '',
        'chase1_max': chase_max,
        'chase2_mode': '',
        'chase2_max': 0,
        'on_unfilled': on_unfilled,
        'fill_wait_sec': fill_wait,
        'max_attempts': max(max_attempts, 1),
    }


def manual_chase_enabled(row: SessionRow) -> bool:
    return (
        (row.on_unfilled or '').lower() == 'chase_same_trade'
        and int(row.chase1_max or 0) > 0
    )


def apply_plan_metadata(state: Dict[str, Any], row: SessionRow) -> None:
    """Persist session plan choices on trade JSON (manual + MEIC)."""
    from manual_spread.butterfly import is_butterfly_side

    pt_pct = getattr(row, 'profit_target_pct', 0.50)
    if pt_pct is None:
        pt_pct = 0.50
    pt_pct = float(pt_pct)

    state['profit_target_pct'] = pt_pct
    if 'exit_config' not in state or not isinstance(state['exit_config'], dict):
        state['exit_config'] = {}
    state['exit_config']['profit_target_pct'] = pt_pct

    if is_butterfly_side(row.side):
        state['stop_profile'] = 'hold_to_expiry'
        state['plan'] = {
            'stop_profile': 'hold_to_expiry',
            'profit_target_pct': pt_pct,
            'on_unfilled': row.on_unfilled or 'none',
            'fill_wait_sec': int(row.fill_wait_sec or ms_config.OPEN_FILL_POLL_SEC),
            'max_attempts': int(row.max_attempts or 1),
            'strategy': ms_config.STRATEGY,
            'structure_type': (row.width or '').replace('bf:', ''),
        }
        return

    is_debit = (row.side or '').upper() in DEBIT_SIDES or state.get('spread_type') == 'debit'
    spread_type = 'debit' if is_debit else 'credit'
    default_mult = 0.5 if is_debit else 2.0
    mult = float(row.stop_multiplier if row.stop_multiplier is not None else default_mult)
    sl_pct = sl_pct_from_stop_multiplier(mult, spread_type=spread_type)

    if is_debit:
        state['spread_type'] = 'debit'
        state['stop_profile'] = 'manual_debit_spread'
        entry = state.setdefault('entry', {})
        entry['side'] = (row.side or 'PDS').upper()
        deb = float(
            entry.get('net_debit')
            or entry.get('limit_debit')
            or entry.get('net_credit')
            or entry.get('limit_credit')
            or row.limit_credit
            or 0.0
        )
        if deb > 0:
            entry['debit'] = deb
            entry['net_debit'] = deb
            entry.setdefault('limit_debit', float(row.limit_credit or deb))

    state['stop_multiplier'] = mult
    state['stop_loss_pct'] = sl_pct
    state['exit_config']['stop_loss_pct'] = sl_pct
    state['on_unfilled'] = row.on_unfilled or 'none'
    state['plan'] = {
        'stop_multiplier': mult,
        'stop_loss_pct': sl_pct,
        'profit_target_pct': pt_pct,
        'on_unfilled': row.on_unfilled or 'none',
        'fill_wait_sec': int(row.fill_wait_sec or ms_config.OPEN_FILL_POLL_SEC),
        'chase_floor': float(row.credit_min or 0),
        'chase_max_attempts': int(row.chase1_max or 0),
        'max_attempts': int(row.max_attempts or 1),
        'strategy': ms_config.STRATEGY,
        'spread_type': spread_type,
    }
