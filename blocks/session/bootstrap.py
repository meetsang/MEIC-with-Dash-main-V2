"""Bootstrap daily session CSV from strategy defaults."""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import meic0dte.app.config as meic_config
from blocks.session.plan import SessionPlan, SessionRow, format_time_field, meic_session_path
from common import trades_layout
from orchestrator.scheduler import TrancheSlot
from strategies.meic.strategy import MEIC_DEFAULT_PAUSED_LOTS, MEIC_TRANCHE_SLOTS

log = logging.getLogger(__name__)


def _default_rows(slots: List[TrancheSlot], root: Optional[str] = None) -> List[SessionRow]:
    from common.meic_settings import get_meic_settings, get_meic_tranche_credits, is_meic_side_enabled
    from blocks.session.session_defaults import load_session_defaults

    settings = get_meic_settings(root=root)
    saved_defaults = load_session_defaults(root=root)
    rows: List[SessionRow] = []
    for slot in slots:
        for side in ('P', 'C'):
            slot_key = f'{slot.lot}_{side}'
            saved = saved_defaults.get(slot_key) or {}

            # Credit targets (saved overrides or settings-calculated defaults)
            c_min, c_max = get_meic_tranche_credits(slot.lot, side, settings=settings, root=root)
            if 'credit_min' in saved:
                try:
                    c_min = float(saved['credit_min'])
                except (ValueError, TypeError):
                    pass
            if 'credit_max' in saved:
                try:
                    c_max = float(saved['credit_max'])
                except (ValueError, TypeError):
                    pass

            # Window times (saved overrides or slot schedule defaults)
            win_start = saved.get('entry_window_start') or format_time_field(slot.window_start)
            win_end = saved.get('entry_window_end') or format_time_field(slot.window_end)

            qty = int(saved.get('quantity', meic_config.QUANTITY))
            width = str(saved.get('width', f'{meic_config.SPREAD_WIDTH_MIN}-{meic_config.SPREAD_WIDTH_MAX}'))
            stop_mult = float(saved.get('stop_multiplier', 2))

            is_paused = (slot.lot in MEIC_DEFAULT_PAUSED_LOTS) or (not is_meic_side_enabled(side, root=root))
            rows.append(SessionRow(
                slot_key=slot_key,
                lot=slot.lot,
                side=side,
                entry_window_start=win_start,
                entry_window_end=win_end,
                entry_condition='time',
                paused=is_paused,
                skip=False,
                quantity=qty,
                stop_mode='multiplier',
                stop_multiplier=stop_mult,
                stop_percent='',
                width=width,
                credit_min=c_min,
                credit_max=c_max,
                chase1_mode=str(saved.get('chase1_mode', 'chase_same_trade')),
                chase1_max=int(saved.get('chase1_max', 3)),
                chase2_mode=str(saved.get('chase2_mode', 'build_new_strikes')),
                chase2_max=int(saved.get('chase2_max', 7)),
                fill_wait_sec=int(saved.get('fill_wait_sec', meic_config.FILL_WAIT_MAX)),
                max_attempts=10,
                state='pending',
                trade_path='',
            ))
    return rows


def bootstrap_meic_session_if_missing(
    root: Optional[str] = None,
    *,
    strategy_name: str = trades_layout.STRATEGY_MEIC,
    slots: Optional[List[TrancheSlot]] = None,
) -> Optional[str]:
    """Create today's MEIC session CSV if missing. Returns path when created."""
    path = meic_session_path(root=root)
    if os.path.isfile(path):
        return None

    slot_list = slots or list(MEIC_TRANCHE_SLOTS)
    plan = SessionPlan(path, strategy_name, _default_rows(slot_list, root=root))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plan.save()
    log.info('Bootstrapped session plan %s (%d rows)', path, len(plan.rows))
    return path
