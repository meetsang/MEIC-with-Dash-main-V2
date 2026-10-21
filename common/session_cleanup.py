"""Session cleanup — morning (8:20) archives active trades; EOD syncs history only.

Archiving runs at morning only so same-day trades stay in the dashboard for
evening review. See changes/fixes/PREMARKET_CLEANUP.md.
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
        m_iso = re.search(r'(20\d{2}-\d{2}-\d{2})', filename)
        if m_iso:
            try:
                return datetime.strptime(m_iso.group(1), '%Y-%m-%d').date()
            except ValueError:
                pass
    entry = state.get('entry') if isinstance(state.get('entry'), dict) else {}
    ts = str(entry.get('timestamp') or state.get('day') or state.get('date') or '')
    if len(ts) >= 10:
        try:
            return datetime.strptime(ts[:10], '%Y-%m-%d').date()
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
        is_prior = (expiry is not None and expiry < today)
        is_closed = str(state.get('status', '')).lower() == 'closed'
        if should_archive_expiry(expiry, today, mode) or (is_prior and is_closed):
            dest_dir = os.path.join(history_root, today.strftime('%Y-%m-%d'))
            _archive_file(path, dest_dir)
            archived += 1
            log.info(
                'Archived %s (expiry=%s mode=%s)',
                name, expiry.isoformat() if expiry else '?', mode,
            )
        elif name.startswith('session_') and is_prior:
            dest_dir = os.path.join(history_root, today.strftime('%Y-%m-%d'))
            _archive_file(path, dest_dir)
            archived += 1
            log.info('Archived prior session %s (date=%s)', name, today.isoformat())
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


def settle_expired_active_trades(
    active_dir: str,
    root: Optional[str] = None,
    today: Optional[date] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[int, int]:
    """Settle or freeze expired open/closing trades in active/. Returns (settled, frozen)."""
    lg = logger or log
    root = root or _project_root()
    today = today or _central_today()
    settled = 0
    frozen = 0
    if not os.path.isdir(active_dir):
        return settled, frozen

    from blocks.stop import state as state_mod
    from blocks.stop.expiry_gate import try_settle_or_freeze_trade

    for name in sorted(os.listdir(active_dir)):
        if not name.endswith('.json'):
            continue
        path = os.path.join(active_dir, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                state = json.load(f)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            lg.warning('Skip settlement for %s — unreadable (%s)', name, exc)
            continue

        outcome, state = try_settle_or_freeze_trade(state, path=path, root=root)
        if outcome == 'ok':
            continue
        state_mod.save_state(path, state)
        if outcome == 'settled':
            settled += 1
        elif outcome == 'frozen':
            frozen += 1
        lg.info('Expiry settlement %s for %s (date=%s)', outcome, name, today.isoformat())

    return settled, frozen


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

    meic_settled, meic_frozen = settle_expired_active_trades(meic_active, root, today, lg)
    manual_settled, manual_frozen = settle_expired_active_trades(manual_active, root, today, lg)

    mk = ma = uk = ua = 0
    mw: List[str] = []
    uw: List[str] = []
    other_warnings: List[str] = []
    other_settled = 0
    other_frozen = 0
    other_archived = 0
    other_kept = 0

    # Sweep all registered strategy active directories (PERMA_BULL_PCS, SKYLINE, IRON_FLY, etc.)
    for strat in trades_layout.registered_strategies():
        if strat in (trades_layout.STRATEGY_MEIC, trades_layout.STRATEGY_MANUAL):
            continue
        s_act = trades_layout.active_dir_for_strategy(strat, root)
        s_hist = trades_layout.history_dir_for_strategy(strat, root)
        if not os.path.isdir(s_act):
            continue
        st, fr = settle_expired_active_trades(s_act, root, today, lg)
        other_settled += st
        other_frozen += fr
        if mode == 'morning':
            kp, arc, warns = archive_active_trades(s_act, s_hist, today, mode)
            other_kept += kp
            other_archived += arc
            other_warnings.extend(warns)

    if mode == 'morning':
        mk, ma, mw = archive_active_trades(meic_active, meic_history, today, mode)
        uk, ua, uw = archive_active_trades(manual_active, manual_history, today, mode)
    else:
        lg.info('Skipping active/ archive at %s — trades stay visible until morning cleanup', mode)

    summary = {
        'mode': mode,
        'date': today.isoformat(),
        'meic_settled': meic_settled,
        'meic_frozen': meic_frozen,
        'manual_settled': manual_settled,
        'manual_frozen': manual_frozen,
        'meic_kept': mk,
        'meic_archived': ma,
        'manual_kept': uk,
        'manual_archived': ua,
        'total_settled': meic_settled + manual_settled + other_settled,
        'total_archived': ma + ua + other_archived,
        'commands_removed': cmds,
        'warnings': mw + uw + other_warnings,
    }
    lg.info(
        'Cleanup done (%s): MEIC settled=%d frozen=%d archived=%d kept=%d | '
        'Manual settled=%d frozen=%d archived=%d kept=%d | All strategies archived=%d | commands=%d',
        mode, meic_settled, meic_frozen, ma, mk, manual_settled, manual_frozen, ua, uk,
        ma + ua + other_archived, cmds,
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

        try:
            import glob
            from strategies.skyline.settlement import settle_open_trade

            active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, root)
            settled = 0
            for path in glob.glob(os.path.join(active, 'skyline_*.json')):
                if settle_open_trade(path, root, for_date=today):
                    settled += 1
            summary['skyline_settled'] = settled
            if settled:
                lg.info('Skyline EOD settlement: %d trade(s) moved to history', settled)
        except Exception as exc:
            lg.exception('Skyline EOD settlement failed: %s', exc)
            summary['skyline_settlement_error'] = str(exc)

        try:
            import glob
            from strategies.drma_hm.settlement import settle_open_trade as settle_drma_hm

            active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, root)
            settled = 0
            for path in glob.glob(os.path.join(active, 'drma_hm_*.json')):
                if settle_drma_hm(path, root, for_date=today):
                    settled += 1
            summary['drma_hm_settled'] = settled
            if settled:
                lg.info('DRMA_HM EOD settlement: %d trade(s) moved to history', settled)
        except Exception as exc:
            lg.exception('DRMA_HM EOD settlement failed: %s', exc)
            summary['drma_hm_settlement_error'] = str(exc)

        try:
            import glob
            from strategies.mean_reversion.settlement import settle_open_trade as settle_mr

            active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_MEAN_REVERSION, root)
            settled = 0
            for path in glob.glob(os.path.join(active, 'mean_reversion_*.json')):
                if settle_mr(path, root, for_date=today):
                    settled += 1
            summary['mean_reversion_settled'] = settled
            if settled:
                lg.info('Mean Reversion EOD settlement: %d trade(s) moved to history', settled)
        except Exception as exc:
            lg.exception('Mean Reversion EOD settlement failed: %s', exc)
            summary['mean_reversion_settlement_error'] = str(exc)

        try:
            from dashboard.strategy_pnl_csv import export_strategy_daily_pnl_csv
            export_strategy_daily_pnl_csv(root)
            lg.info('EOD: Updated strategy_daily_pnl.csv successfully')
        except Exception as exc:
            lg.warning('EOD strategy_daily_pnl.csv export failed: %s', exc)

    return summary


def central_today() -> date:
    """Today in US Central (for tests and state helpers)."""
    return _central_today()
