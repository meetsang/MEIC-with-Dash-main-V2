"""Companion Iron Condor tranche breach detection and resolution."""
from __future__ import annotations

import glob
import logging
import os
from typing import Any, Dict, Optional

from common import trades_layout
from blocks.stop import state as state_mod

log = logging.getLogger(__name__)


def get_companion_side(side: str) -> str:
    """Opposite side of an Iron Condor tranche ('P' <-> 'C')."""
    return 'C' if side.upper() == 'P' else 'P'


def _extract_trade_date(state: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """Return (YYYY-MM-DD, YYYYMMDD) for the given trade state, or (None, None)."""
    entry_ts = str(state.get('entry', {}).get('timestamp') or '')
    if len(entry_ts) >= 10 and entry_ts[4] == '-' and entry_ts[7] == '-':
        iso_date = entry_ts[:10]
        return iso_date, iso_date.replace('-', '')

    fn = str(state.get('_filename') or '')
    import re
    m = re.search(r'20\d{6}', fn)
    if m:
        compact = m.group(0)
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}", compact

    try:
        from meic0dte.app.utilities import central_today
        td = central_today().isoformat()
        return td, td.replace('-', '')
    except Exception:
        return None, None


def find_companion_trade_state(
    state: Dict[str, Any],
    *,
    root: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    Find companion trade state (opposite side of the same tranche) in active or history dirs.
    Strictly scoped to the same session date to prevent picking up past archives.
    """
    strategy = (
        state.get('entry', {}).get('strategy')
        or state.get('strategy')
        or trades_layout.STRATEGY_MEIC
    )
    lot = state.get('lot')
    side = (
        state.get('entry', {}).get('side')
        or ('P' if '_P' in str(state.get('slot_key', '')) else 'C')
    )
    if not lot or not side:
        return None

    iso_date, compact_date = _extract_trade_date(state)
    companion_side = get_companion_side(side)
    base = root or state_mod._project_root()
    active_dir = trades_layout.active_dir_for_strategy(strategy, root=base)

    prefix = f"{lot}_{companion_side}_"
    # 1. Check active directory
    if os.path.isdir(active_dir):
        for path in glob.glob(os.path.join(active_dir, f"{prefix}*.json")):
            try:
                st = state_mod.load_state(path)
                if st.get('lot') != lot:
                    continue
                if compact_date and compact_date not in os.path.basename(path):
                    st_iso, _ = _extract_trade_date(st)
                    if st_iso and st_iso != iso_date:
                        continue
                return st
            except Exception:
                pass

    # 2. Check history directory (strictly for the same session date)
    history_dir = trades_layout.history_dir_for_strategy(strategy, root=base)
    if os.path.isdir(history_dir):
        patterns = []
        if compact_date:
            patterns.append(os.path.join(history_dir, f"{prefix}*{compact_date}*.json"))
            patterns.append(os.path.join(history_dir, '**', f"{prefix}*{compact_date}*.json"))
        if iso_date:
            patterns.append(os.path.join(history_dir, iso_date, f"{prefix}*.json"))
            patterns.append(os.path.join(history_dir, '**', iso_date, f"{prefix}*.json"))
        if not patterns:
            patterns.append(os.path.join(history_dir, '**', f"{prefix}*.json"))

        for pat in patterns:
            for path in glob.glob(pat, recursive=True):
                try:
                    st = state_mod.load_state(path)
                    if st.get('lot') != lot:
                        continue
                    # Ensure candidate matches the same session date
                    st_iso, st_compact = _extract_trade_date(st)
                    if iso_date and st_iso and st_iso != iso_date:
                        continue
                    if compact_date and compact_date not in path and st_compact != compact_date:
                        continue
                    return st
                except Exception:
                    pass

    return None


def is_companion_breached(companion_state: Optional[Dict[str, Any]]) -> bool:
    """
    True if the companion tranche side has breached (software breach or stop fill).
    Note: Profit target hits (profit_target_oco) are winning closes and are NOT breaches.
    Unentered or unfilled companion trades (e.g. cancelled before fill) are NOT breaches.
    """
    if not companion_state:
        return False

    # If companion never filled any quantity, it was never an active position
    if companion_state.get('filled_quantity', 1) == 0:
        return False

    status = str(companion_state.get('status') or '').lower()
    if status in ('pending_fill', 'failed', 'cancelled'):
        if companion_state.get('short_closed_at') is None:
            return False

    short_fill = float(companion_state.get('short_leg', {}).get('fill_price') or 0.0)
    if short_fill <= 0.0 and companion_state.get('short_closed_at') is None:
        return False

    close_mech = str(companion_state.get('close_mechanism') or '').lower()
    if close_mech in (
        'software_breach',
        'exchange_stop',
        'breach_limit_reprice',
        'spread_stop_breach',
        'phase3_proximity',
    ):
        return True

    phases = companion_state.get('phases') or {}
    if phases.get('breach_limit_placed_at') is not None:
        return True

    active_stop = companion_state.get('active_stop') or {}
    if active_stop.get('type') == 'LIMIT' and active_stop.get('order_id'):
        return True

    if companion_state.get('short_closed_at') is not None and close_mech not in (
        'profit_target_oco',
        'manual_close',
    ):
        return True

    return False
