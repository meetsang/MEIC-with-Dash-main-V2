"""Trade state JSON persistence for stop_monitor."""
from __future__ import annotations

import glob
import json
import os
import shutil
import tempfile
import time
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

from common import tt_config
from common import trades_layout


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


def active_dir() -> str:
    return os.path.join(_project_root(), tt_config.TRADES_ACTIVE_DIR)


def manual_spread_active_dir() -> str:
    return os.path.join(_project_root(), tt_config.MANUAL_SPREAD_ACTIVE_DIR)


def manual_spread_closed_dir() -> str:
    return os.path.join(_project_root(), tt_config.MANUAL_SPREAD_CLOSED_DIR)


def closed_dir_for_state(state: Dict[str, Any]) -> str:
    strategy = section(state, 'entry').get('strategy', '')
    if strategy == trades_layout.STRATEGY_MANUAL:
        return manual_spread_closed_dir()
    return closed_dir()


def all_active_dirs() -> List[str]:
    """MEIC and Manual Spread active trade directories (deduped)."""
    return trades_layout.all_active_dirs(_project_root())


def iter_active_trade_paths() -> Iterator[str]:
    """Yield every trades/active/{strategy}/*.json path."""
    for directory in all_active_dirs():
        if not os.path.isdir(directory):
            continue
        yield from glob.glob(os.path.join(directory, '*.json'))


def closed_dir() -> str:
    return os.path.join(_project_root(), tt_config.TRADES_CLOSED_DIR)


def ensure_dirs() -> None:
    trades_layout.ensure_all_trade_dirs(_project_root())


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec='seconds')


def entry_ts_compact(when: Optional[datetime] = None) -> str:
    """Compact entry timestamp for stable trade filenames (e.g. 20260625T131403)."""
    dt = when or datetime.now().astimezone()
    return dt.strftime('%Y%m%dT%H%M%S')


def stable_trade_filename(lot: str, side: str, entry_ts: Optional[str] = None) -> str:
    """One JSON per lot+side per session entry — order id is not in the filename."""
    ts = entry_ts or entry_ts_compact()
    return f'{lot}_{side.upper()}_{ts}.json'


def section(state: Dict[str, Any], key: str) -> Dict[str, Any]:
    """Nested dict under key; JSON null or missing keys become {}."""
    val = state.get(key)
    return val if isinstance(val, dict) else {}


def _v2_metadata(strategy: str) -> Dict[str, Any]:
    from blocks.stop.profiles.meic import MEIC_CREDIT_SPREAD_PROFILE

    return {
        'strategy_version': '2.0',
        'instrument': 'SPX',
        'spread_type': 'credit',
        'stop_profile': MEIC_CREDIT_SPREAD_PROFILE,
    }


def create_pending_state(
    *,
    strategy: str,
    lot: str,
    side: str,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    target_quantity: int,
    open_order_id: str,
    limit_credit: float = 0.0,
) -> Dict[str, Any]:
    """Minimal handshake JSON after order place — stop_monitor syncs fills from open_order_id."""
    meta = _v2_metadata(strategy)
    return {
        **meta,
        'status': 'pending_fill',
        'lot': lot,
        'quantity': target_quantity,
        'filled_quantity': 0,
        'stop_quantity': 0,
        'open_order_id': open_order_id,
        'open_order': {
            'status': 'working',
            'last_sync': now_iso(),
            'last_sync_epoch': 0,
            'fully_filled': False,
            'fill_sync': {
                'schema_version': 2,
                'phase': 'fast',
                'poll_count': 0,
                'confirm_attempted': False,
                'next_poll_epoch': 0.0,
                'audit_due_epoch': None,
                'audit_attempted': False,
            },
        },
        'entry': {
            'strategy': strategy,
            'lot': lot,
            'side': side,
            'timestamp': now_iso(),
            'net_credit': limit_credit,
            'two_x_net_credit': 0.0,
            'limit_credit': limit_credit,
        },
        'short_leg': {
            'symbol': short_symbol,
            'strike': short_strike,
            'fill_price': 0.0,
            'two_x_short': 0.0,
        },
        'long_leg': {
            'symbol': long_symbol,
            'strike': long_strike,
            'fill_price': 0.0,
        },
        'active_stop': None,
        'stop_history': [],
        'order_history': [],
        'phases': {
            'phase1_active': True,
            'phase2_activated_at': None,
            'phase3_activated_at': None,
            'short_stoplmt_replaced': False,
        },
        'long_close_order_id': None,
        'long_close_price': None,
        'short_close_price': None,
        'close_mechanism': None,
        'close': None,
        'recovery': {
            'module_start_count': 0,
            'last_heartbeat': now_iso(),
            'state_loaded_from_disk': False,
        },
    }


def create_new_state(
    *,
    strategy: str,
    lot: str,
    side: str,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    short_fill: float,
    long_fill: float,
    net_credit: float,
    quantity: int,
    open_order_id: str,
) -> Dict[str, Any]:
    """Build initial state dict before stop_monitor places the first stop."""
    two_x_short = round(round(short_fill * 2.0 / 0.05) * 0.05, 2)
    two_x_credit = round(round(net_credit * 2.0 / 0.05) * 0.05, 2)
    meta = _v2_metadata(strategy)
    return {
        **meta,
        'status': 'open',
        'lot': lot,
        'quantity': quantity,
        'filled_quantity': quantity,
        'stop_quantity': 0,
        'open_order_id': open_order_id,
        'open_order': {
            'status': 'filled',
            'last_sync': now_iso(),
            'last_sync_epoch': 0,
            'fully_filled': True,
        },
        'entry': {
            'strategy': strategy,
            'lot': lot,
            'side': side,
            'timestamp': now_iso(),
            'net_credit': net_credit,
            'two_x_net_credit': two_x_credit,
        },
        'short_leg': {
            'symbol': short_symbol,
            'strike': short_strike,
            'fill_price': short_fill,
            'two_x_short': two_x_short,
        },
        'long_leg': {
            'symbol': long_symbol,
            'strike': long_strike,
            'fill_price': long_fill,
        },
        'active_stop': None,
        'stop_history': [],
        'order_history': [],
        'phases': {
            'phase1_active': True,
            'phase2_activated_at': None,
            'phase3_activated_at': None,
            'short_stoplmt_replaced': False,
        },
        'long_close_order_id': None,
        'long_close_price': None,
        'short_close_price': None,
        'close_mechanism': None,
        'close': None,
        'recovery': {
            'module_start_count': 0,
            'last_heartbeat': now_iso(),
            'state_loaded_from_disk': False,
        },
    }


def state_filename(
    strategy: str,
    lot: str,
    side: str,
    *,
    date_str: Optional[str] = None,
    open_order_id: Optional[str] = None,
    entry_hhmm: Optional[str] = None,
) -> str:
    """
    One file per tranche trade. Includes lot (e.g. 11-00), entry HHMM, side, and
    open order tail so a later re-entry on the same strikes is not overwritten.
    """
    date_str = date_str or datetime.now().strftime('%y%m%d')
    safe_lot = lot.replace('-', '')
    hhmm = entry_hhmm or datetime.now().astimezone().strftime('%H%M')
    base = f'{strategy}_SPX_{date_str}_{safe_lot}_{hhmm}_{side.upper()}'
    oid = ''.join(c for c in str(open_order_id or '') if c.isdigit())
    if oid and str(open_order_id) not in ('manual-seed',):
        base = f'{base}_{oid[-6:]}'
    return f'{base}.json'


def active_path_glob() -> str:
    return os.path.join(active_dir(), '*.json')


def save_state(path: str, state: Dict[str, Any]) -> None:
    """Atomic write via temp file + rename (retries replace on Windows)."""
    state['recovery']['last_heartbeat'] = now_iso()
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        for attempt in range(8):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def load_state(path: str, *, retries: int = 8, delay: float = 0.05) -> Dict[str, Any]:
    """Load JSON state; retries when another thread is mid-atomic-save (Windows)."""
    last_err: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except PermissionError as exc:
            last_err = exc
            time.sleep(delay * (attempt + 1))
    if last_err is not None:
        raise last_err
    raise FileNotFoundError(path)


def append_stop_history(
    state: Dict[str, Any],
    *,
    action: str,
    order_id: Optional[str],
    price: Optional[float],
    phase: int,
    reason: str,
    spx_price_at_event: Optional[float] = None,
    status: Optional[str] = None,
) -> None:
    entry = {
        'action': action,
        'order_id': order_id,
        'price': price,
        'phase': phase,
        'reason': reason,
        'timestamp': now_iso(),
    }
    if spx_price_at_event is not None:
        entry['spx_price_at_event'] = spx_price_at_event
    if status is not None:
        entry['status'] = status
    state.setdefault('stop_history', []).append(entry)


def append_order_history(
    state: Dict[str, Any],
    *,
    order_id: str,
    limit_credit: float,
    short_strike: int,
    long_strike: int,
    status: str = 'working',
    filled_quantity: int = 0,
    reason: str = 'placed',
    on_unfilled_step: Optional[str] = None,
) -> None:
    entry: Dict[str, Any] = {
        'order_id': str(order_id),
        'limit_credit': limit_credit,
        'short_strike': short_strike,
        'long_strike': long_strike,
        'status': status,
        'filled_quantity': filled_quantity,
        'ts': now_iso(),
        'reason': reason,
    }
    if on_unfilled_step is not None:
        entry['on_unfilled_step'] = on_unfilled_step
    state.setdefault('order_history', []).append(entry)


def update_pending_order(
    state: Dict[str, Any],
    *,
    open_order_id: str,
    limit_credit: float,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    target_quantity: int,
) -> None:
    """Refresh handshake fields when re-placing on the same trade JSON."""
    state['open_order_id'] = open_order_id
    state['quantity'] = target_quantity
    state['open_order'] = {
        'status': 'working',
        'last_sync': now_iso(),
        'last_sync_epoch': 0,
        'fully_filled': False,
    }
    state['entry']['limit_credit'] = limit_credit
    state['entry']['net_credit'] = limit_credit
    state['short_leg']['symbol'] = short_symbol
    state['short_leg']['strike'] = short_strike
    state['short_leg']['fill_price'] = 0.0
    state['long_leg']['symbol'] = long_symbol
    state['long_leg']['strike'] = long_strike
    state['long_leg']['fill_price'] = 0.0
    if state.get('status') not in ('open', 'closing', 'closed'):
        state['status'] = 'pending_fill'


def find_active_trade_for_slot(
    lot: str,
    side: str,
    *,
    strategy: str = trades_layout.STRATEGY_MEIC,
) -> Optional[str]:
    """Return active JSON path for lot+side, preferring live (non-closed) trades."""
    dest = trades_layout.active_dir_for_strategy(strategy, _project_root())
    if not os.path.isdir(dest):
        return None
    side = side.upper()
    matches: List[str] = []
    for name in os.listdir(dest):
        if not name.endswith('.json'):
            continue
        path = os.path.join(dest, name)
        try:
            st = load_state(path)
        except Exception:
            continue
        trade_lot = st.get('lot') or section(st, 'entry').get('lot', '')
        trade_side = section(st, 'entry').get('side', '').upper()
        if trade_lot != lot or trade_side != side:
            continue
        if st.get('status') == 'closed':
            continue
        matches.append(path)
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]

    def _sort_key(path: str) -> tuple:
        st = load_state(path)
        filled = int(st.get('filled_quantity') or 0)
        status = st.get('status', '')
        rank = {'open': 3, 'closing': 3, 'pending_fill': 1}.get(status, 2)
        ts = section(st, 'entry').get('timestamp') or ''
        return (filled > 0, rank, ts)

    return max(matches, key=_sort_key)


def find_active_by_strikes(
    side: str,
    short_strike: int,
    long_strike: int,
) -> Optional[str]:
    """Return active JSON path matching put/call side and short/long strikes."""
    side = side.upper()
    for name in os.listdir(active_dir()):
        if not name.endswith('.json'):
            continue
        path = os.path.join(active_dir(), name)
        try:
            st = load_state(path)
        except Exception:
            continue
        if section(st, 'entry').get('side', '').upper() != side:
            continue
        if int(section(st, 'short_leg').get('strike') or 0) != int(short_strike):
            continue
        if int(section(st, 'long_leg').get('strike') or 0) != int(long_strike):
            continue
        return path
    return None


def mark_manual_close(
    active_path: str,
    *,
    close_mechanism: str = 'manual_close',
    reason: Optional[str] = None,
) -> str:
    """
    Record a spread closed outside the bot and move JSON out of trades/active/.

    stop_monitor only watches active/*.json — removing the file prevents stops on restart.
    """
    state = load_state(active_path)
    close_reason = reason or close_mechanism
    state['status'] = 'closed'
    state['close_mechanism'] = close_mechanism
    state['active_stop'] = None
    state['stop_quantity'] = 0
    state['close'] = {
        'reason': close_reason,
        'timestamp': now_iso(),
        'entry_credit': section(state, 'entry').get('net_credit'),
        'short_fill': section(state, 'short_leg').get('fill_price'),
        'long_fill': section(state, 'long_leg').get('fill_price'),
        'short_close_price': state.get('short_close_price'),
        'long_close_price': state.get('long_close_price'),
        'close_mechanism': close_mechanism,
        'spx_at_close': None,
        'manual': True,
    }
    return move_to_closed(active_path, state)


def move_to_closed(active_path: str, state: Dict[str, Any]) -> str:
    ensure_dirs()
    basename = os.path.basename(active_path)
    dest_root = closed_dir_for_state(state)
    closed_path = os.path.join(dest_root, basename)
    save_state(closed_path, state)
    if os.path.exists(active_path):
        os.unlink(active_path)
    return closed_path


def archive_daily_trades(base_dir: Optional[str] = None) -> None:
    """Move eligible active trades to history/YYYY-MM-DD/ (morning rules only).

    Prefer run_session_cleanup('morning') from run.py for full cleanup.
    """
    from common.session_cleanup import archive_active_trades, central_today

    today = central_today()
    root = _project_root()
    if base_dir is None:
        meic_active = active_dir()
        meic_history = closed_dir()
        manual_active = manual_spread_active_dir()
        manual_history = manual_spread_closed_dir()
    else:
        meic_active = os.path.join(base_dir, 'active')
        meic_history = os.path.join(base_dir, 'history')
        manual_active = manual_spread_active_dir()
        manual_history = manual_spread_closed_dir()

    archive_active_trades(meic_active, meic_history, today, 'morning')
    if os.path.normpath(manual_active) != os.path.normpath(meic_active):
        archive_active_trades(manual_active, manual_history, today, 'morning')
