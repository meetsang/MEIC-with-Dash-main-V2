"""Session cleanup — morning (8:20) archives active trades; EOD syncs history only.

Archiving runs at morning only so same-day trades stay in the dashboard for
evening review. See changes/PREMARKET_CLEANUP.md.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from common import tt_config
from common import trades_layout
from common.symbols import parse_canonical

log = logging.getLogger(__name__)

_FILENAME_EXPIRY_RE = re.compile(
    r'_(SPX|SPXW)_(\d{6})_', re.IGNORECASE
)


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _central_today() -> date:
    from meic0dte.app.utilities import central_now

    return central_now().date()


def trade_expiry_date(state: Dict[str, Any], filename: str = '') -> Optional[date]:
    """Option expiry from leg symbols, else YYMMDD embedded in filename."""
    for key in ('short_leg', 'long_leg'):
        leg = state.get(key) if isinstance(state.get(key), dict) else {}
        sym = leg.get('symbol') or ''
        parsed = parse_canonical(str(sym))
        if parsed:
            try:
                return datetime.strptime(parsed[0], '%y%m%d').date()
            except ValueError:
                pass
    if filename:
        m = _FILENAME_EXPIRY_RE.search(filename)
        if m:
            try:
                return datetime.strptime(m.group(2), '%y%m%d').date()
            except ValueError:
                pass
    return None


def should_archive_expiry(
    expiry: Optional[date],
    today: date,
    mode: str,
) -> bool:
    """
    morning: archive only if expiry < today (expired prior days).
    eod: never archives — active/ cleanup runs at morning only.
    """
    if mode == 'eod':
        return False
    if mode == 'morning':
        if expiry is None:
            return False
        return expiry < today
    raise ValueError(f'unknown cleanup mode: {mode}')


def _archive_file(src: str, dest_dir: str) -> None:
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(src))
    if os.path.exists(dest):
        base, ext = os.path.splitext(os.path.basename(src))
        dest = os.path.join(dest_dir, f'{base}_{int(datetime.now().timestamp())}{ext}')
    os.replace(src, dest)


def archive_active_trades(
    active_dir: str,
    history_root: str,
    today: date,
    mode: str,
) -> Tuple[int, int, List[str]]:
    """Move eligible active/*.json into history_root/YYYY-MM-DD/. Returns kept, archived, warnings."""
    if not os.path.isdir(active_dir):
        return 0, 0, []
    dest_dir = os.path.join(history_root, today.strftime('%Y-%m-%d'))
    archived = 0
    kept = 0
    warnings: List[str] = []
    for name in sorted(os.listdir(active_dir)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(active_dir, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f'{name}: unreadable ({exc})')
            kept += 1
            continue
        expiry = trade_expiry_date(state, name)
        if should_archive_expiry(expiry, today, mode):
            _archive_file(path, dest_dir)
            archived += 1
            log.info(
                'Archived %s (expiry=%s mode=%s)',
                name, expiry.isoformat() if expiry else '?', mode,
            )
        else:
            kept += 1
            log.debug(
                'Keeping %s (expiry=%s mode=%s)',
                name, expiry.isoformat() if expiry else '?', mode,
            )
    return kept, archived, warnings


def reset_optsymbols(root: Optional[str] = None) -> None:
    root = root or _project_root()
    path = os.path.join(root, 'streaming', 'optsymbols.json')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'SYMBOLS': []}, f, indent=2)
    log.info('Reset %s', path)


def clear_pause_tranches(root: Optional[str] = None) -> None:
    """No-op — MEIC pause state lives in session CSV (retired pause_tranches.json)."""
    log.debug('clear_pause_tranches skipped (session CSV owns pause state)')


def clear_killswitch(root: Optional[str] = None) -> None:
    root = root or _project_root()
    path = trades_layout.ops_path('killswitch.json', root)
    if os.path.isfile(path):
        os.remove(path)
        log.info('Removed killswitch.json')


def clear_command_files(root: Optional[str] = None) -> int:
    root = root or _project_root()
    removed = 0
    sub = trades_layout.commands_dir(root)
    if os.path.isdir(sub):
        for path in glob.glob(os.path.join(sub, '*.json')):
            try:
                os.remove(path)
                removed += 1
            except OSError as exc:
                log.warning('Could not remove %s: %s', path, exc)
    if removed:
        log.info('Removed %d stale command file(s)', removed)
    return removed


def run_session_cleanup(mode: str, logger: Optional[logging.Logger] = None) -> Dict[str, Any]:
    """
    Run full cleanup pass.

    mode: 'morning' (8:20 CT) archives prior-expiry active JSON; 'eod' (3:30 CT)
    syncs history only — no active/ archive (evening dashboard review).
    """
    lg = logger or log
    if mode not in ('morning', 'eod'):
        raise ValueError("mode must be 'morning' or 'eod'")

    root = _project_root()
    today = _central_today()

    meic_active = os.path.join(root, tt_config.TRADES_ACTIVE_DIR)
    meic_history = os.path.join(root, tt_config.TRADES_CLOSED_DIR)
    manual_active = os.path.join(root, tt_config.MANUAL_SPREAD_ACTIVE_DIR)
    manual_history = os.path.join(root, tt_config.MANUAL_SPREAD_CLOSED_DIR)

    lg.info('Session cleanup (%s) — Central date %s', mode, today.isoformat())

    reset_optsymbols(root)
    clear_killswitch(root)
    cmds = clear_command_files(root)

    try:
        from blocks.session.bootstrap import bootstrap_meic_session_if_missing
        bootstrap_meic_session_if_missing(root)
    except Exception as exc:
        lg.warning('Session bootstrap failed: %s', exc)

    if mode == 'morning':
        mk, ma, mw = archive_active_trades(meic_active, meic_history, today, mode)
        uk, ua, uw = archive_active_trades(manual_active, manual_history, today, mode)
    else:
        mk = ma = uk = ua = 0
        mw = uw = []
        lg.info('Skipping active/ archive at %s — trades stay visible until morning cleanup', mode)

    summary = {
        'mode': mode,
        'date': today.isoformat(),
        'meic_kept': mk,
        'meic_archived': ma,
        'manual_kept': uk,
        'manual_archived': ua,
        'commands_removed': cmds,
        'warnings': mw + uw,
    }
    lg.info(
        'Cleanup done (%s): MEIC archived=%d kept=%d | Manual archived=%d kept=%d | commands=%d',
        mode, ma, mk, ua, uk, cmds,
    )
    for w in summary['warnings']:
        lg.warning('Cleanup: %s', w)

    if mode == 'eod':
        try:
            from dashboard.history_sync import sync_history_from_disk
            from common.expiry_settlement import ensure_spx_settlement_close

            spx = ensure_spx_settlement_close(today, root=root)
            hist = sync_history_from_disk(root, for_date=today, spx_close=spx)
            summary['history_sync'] = hist
            summary['spx_settlement'] = spx
            lg.info('EOD history sync: synced=%s spx=%s', hist.get('synced'), spx)
        except Exception as exc:
            lg.exception('EOD history sync failed: %s', exc)
            summary['history_sync_error'] = str(exc)

    return summary


def central_today() -> date:
    """Today in US Central (for tests and state helpers)."""
    return _central_today()
