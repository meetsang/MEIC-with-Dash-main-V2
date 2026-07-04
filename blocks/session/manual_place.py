"""Manual place-order plan fields — session CSV + trade JSON."""
from __future__ import annotations

from typing import Any, Dict

import meic0dte.app.config as meic_config
from blocks.session.plan import SessionRow
from manual_spread import config as ms_config


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

    return {
        'stop_multiplier': float(data.get('stop_multiplier', 2)),
        'width': '',
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
    state['stop_multiplier'] = float(row.stop_multiplier or 2)
    state['on_unfilled'] = row.on_unfilled or 'none'
    state['plan'] = {
        'stop_multiplier': float(row.stop_multiplier or 2),
        'on_unfilled': row.on_unfilled or 'none',
        'fill_wait_sec': int(row.fill_wait_sec or ms_config.OPEN_FILL_POLL_SEC),
        'chase_floor': float(row.credit_min or 0),
        'chase_max_attempts': int(row.chase1_max or 0),
        'max_attempts': int(row.max_attempts or 1),
        'strategy': ms_config.STRATEGY,
    }
