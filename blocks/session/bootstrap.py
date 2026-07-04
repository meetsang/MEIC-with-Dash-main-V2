"""Bootstrap daily session CSV from strategy defaults."""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import meic0dte.app.config as meic_config
from blocks.session.plan import SessionPlan, SessionRow, format_time_field, meic_session_path
from common import trades_layout
from orchestrator.scheduler import TrancheSlot
from strategies.meic.strategy import MEIC_TRANCHE_SLOTS

log = logging.getLogger(__name__)


def _default_rows(slots: List[TrancheSlot]) -> List[SessionRow]:
    rows: List[SessionRow] = []
    for slot in slots:
        for side in ('P', 'C'):
            rows.append(SessionRow(
                slot_key=f'{slot.lot}_{side}',
                lot=slot.lot,
                side=side,
                entry_window_start=format_time_field(slot.window_start),
                entry_window_end=format_time_field(slot.window_end),
                entry_condition='time',
                paused=False,
                skip=False,
                quantity=meic_config.QUANTITY,
                stop_mode='multiplier',
                stop_multiplier=2,
                stop_percent='',
                width=f'{meic_config.SPREAD_WIDTH_MIN}-{meic_config.SPREAD_WIDTH_MAX}',
                credit_min=meic_config.CREDIT_MIN,
                credit_max=meic_config.CREDIT_MAX_P,
                chase1_mode='chase_same_trade',
                chase1_max=3,
                chase2_mode='build_new_strikes',
                chase2_max=7,
                fill_wait_sec=meic_config.FILL_WAIT_MAX,
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
    plan = SessionPlan(path, strategy_name, _default_rows(slot_list))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    plan.save()
    log.info('Bootstrapped session plan %s (%d rows)', path, len(plan.rows))
    return path
