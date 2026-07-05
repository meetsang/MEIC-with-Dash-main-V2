"""Dashboard command claiming (V3 §6.6)."""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from blocks.stop import state as state_mod
from blocks.stop.monitor import _trades_dir, _trades_root_for_path
from blocks.stop.v3.recovery import ensure_v3_exit_fields, mark_exit_started
from blocks.stop.v3.trade_slot import TradeSlot, save_slot

log = logging.getLogger(__name__)


def _claim_path(cmd_path: str) -> str:
    pid = os.getpid()
    ts = int(time.time() * 1000)
    claimed = f'{cmd_path}.processing.{pid}.{ts}.json'
    os.replace(cmd_path, claimed)
    return claimed


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _archive_claimed(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def check_killswitch_global() -> bool:
    return os.path.exists(os.path.join(_trades_dir(), 'killswitch.json'))


def claim_killswitch() -> bool:
    ks_path = os.path.join(_trades_dir(), 'killswitch.json')
    if not os.path.exists(ks_path):
        return False
    try:
        _claim_path(ks_path)
        log.info('Claimed global killswitch')
        return True
    except OSError as exc:
        log.warning('Killswitch claim failed: %s', exc)
        return False


def claim_manual_close(
    slot: TradeSlot,
    *,
    mechanism: str = 'manual_close',
) -> bool:
    """Persist close_only_mode + exit fields, return True if newly claimed."""
    if slot.status in ('closed', 'cancelled'):
        return False
    if slot.close_only_mode or slot.exit_job_id:
        log.debug('Manual close duplicate ignored for %s', slot.path)
        return False

    ensure_v3_exit_fields(slot.state, mechanism=mechanism)
    mark_exit_started(slot.state, step='manual_kill_claimed', mechanism=mechanism)
    slot.state['close_mechanism'] = mechanism
    save_slot(slot)
    return True


def detect_and_claim_close_command(slot: TradeSlot) -> Tuple[bool, str]:
    """
    Per-trade .close.json — claim file and persist exit state.
    Returns (claimed, mechanism).
    """
    filename = os.path.basename(slot.path)
    trades_root = _trades_root_for_path(slot.path)
    cmd_path = os.path.join(trades_root, 'commands', f'{filename}.close.json')
    if not os.path.exists(cmd_path):
        return False, 'manual_close'

    cmd = _read_json(cmd_path)
    mechanism = str(cmd.get('close_mechanism') or 'manual_close')
    try:
        claimed = _claim_path(cmd_path)
    except OSError as exc:
        log.warning('Close command claim failed for %s: %s', filename, exc)
        return False, mechanism

    if not claim_manual_close(slot, mechanism=mechanism):
        _archive_claimed(claimed)
        return False, mechanism

    _archive_claimed(claimed)
    log.info('Claimed manual close for %s mechanism=%s', filename, mechanism)
    return True, mechanism


def apply_killswitch_to_slot(slot: TradeSlot) -> bool:
    if slot.status != 'open':
        return False
    return claim_manual_close(slot, mechanism='admin_killswitch')
