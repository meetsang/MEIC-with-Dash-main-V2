"""Poll broker for open-order fills on trades not yet handed to stop_monitor."""
from __future__ import annotations

import logging
import os
import time
from typing import List

from blocks.stop import state as state_mod
from blocks.stop.fill_provenance import ensure_fill_sync, is_fill_sync_terminal
from blocks.stop.fill_sync import sync_open_order
from brokers.base import BrokerBase
from common.streamer_symbols import register_spread_symbols

log = logging.getLogger(__name__)


def needs_open_order_sync(state: dict) -> bool:
    """True when brokerage open order may still need fill prices synced."""
    status = state.get('status')
    if status not in ('pending_fill', 'open'):
        return False
    if not state.get('open_order_id'):
        return False

    fs = ensure_fill_sync(state)
    phase = fs.get('phase', 'fast')
    if phase == 'resolved_estimated' and not fs.get('audit_attempted'):
        due = fs.get('audit_due_epoch')
        if due is not None and time.time() >= float(due):
            return True

    if is_fill_sync_terminal(state):
        return False

    short_px = float((state.get('short_leg') or {}).get('fill_price') or 0)
    long_px = float((state.get('long_leg') or {}).get('fill_price') or 0)
    if short_px > 0 and long_px > 0:
        filled = int(state.get('filled_quantity') or 0)
        target = int(state.get('quantity') or 0)
        if target and filled >= target:
            open_order = state.get('open_order') or {}
            if open_order.get('fully_filled'):
                return False
    return True


def sync_pending_fills(
    broker: BrokerBase,
    *,
    force: bool = False,
) -> List[str]:
    """
    Sync all active trades waiting on open-order fills.

    Returns paths that were updated. Stop monitor picks up promoted trades on its
    next scan once status becomes open with leg fills.
    """
    changed_paths: List[str] = []
    for path in state_mod.iter_active_trade_paths():
        try:
            state = state_mod.load_state(path)
        except (OSError, ValueError) as exc:
            log.warning('Skip pending fill sync for %s: %s', path, exc)
            continue
        if not needs_open_order_sync(state):
            continue
        prev_status = state.get('status')
        prev_filled = int(state.get('filled_quantity') or 0)
        changed, result = sync_open_order(state, broker, force=force)
        if not changed:
            continue
        state_mod.save_state(path, state)
        changed_paths.append(path)
        lot = state.get('lot', '?')
        filled = int(state.get('filled_quantity') or 0)
        log.info(
            'Pending fill sync %s: %s qty %s status %s→%s filled %s→%s',
            lot,
            os.path.basename(path),
            state.get('quantity'),
            prev_status,
            state.get('status'),
            prev_filled,
            filled,
        )
        if state.get('status') == 'open':
            register_spread_symbols(state, lot, log)
        if result and not result.success:
            log.warning('Pending fill sync %s broker error: %s', lot, result.message)
    return changed_paths
