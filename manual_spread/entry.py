"""Manual Spread entry: scan, place, modify, cancel."""
from __future__ import annotations

import json
import logging
import os
import shutil
import time
from datetime import datetime as dt
from typing import Any, Dict, List, Optional

from blocks.entry.config import CreditEntryConfig
from blocks.entry.credit_spread import CreditSpreadEntry
from common.streamer_symbols import register_spread_symbols
from common.strike_guard import leg_overlap_conflict, resolve_leg_overlap
from common.symbols import build_tastytrade_symbol, parse_canonical, to_tastytrade
from manual_spread import config as ms_config
from blocks.entry.spread_scan import SpreadCandidate, resolve_scan_otm_max
from blocks.stop import state as state_mod
from blocks.stop.fill_sync import sync_open_order

log = logging.getLogger(__name__)


def _manual_entry(broker) -> CreditSpreadEntry:
    cfg = CreditEntryConfig(
        otm_min=ms_config.OTM_MIN,
        otm_max=ms_config.OTM_MAX_LOW_TARGET,
        credit_min=ms_config.MIN_MARKET_CREDIT,
        min_market_credit=ms_config.MIN_MARKET_CREDIT,
        quote_source='api',
    )
    return CreditSpreadEntry(broker, cfg, log=log)


def _counter_path() -> str:
    from common import trades_layout
    return trades_layout.ops_path('manual_counter.json')


def _commands_dir() -> str:
    from common import trades_layout
    path = trades_layout.commands_dir()
    os.makedirs(path, exist_ok=True)
    return path


def next_lot() -> str:
    path = _counter_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        data = {'next': 1}
    n = int(data.get('next', 1))
    data['next'] = n + 1
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)
    return f'ms-{n}'


def parse_expiry(expiry: str) -> str:
    """Convert YYYY-MM-DD or YYMMDD to YYMMDD."""
    expiry = (expiry or '').strip()
    if len(expiry) == 10 and '-' in expiry:
        return dt.strptime(expiry, '%Y-%m-%d').strftime('%y%m%d')
    if len(expiry) == 8 and expiry.isdigit():
        return expiry
    if len(expiry) == 6 and expiry.isdigit():
        return expiry
    raise ValueError(f'Invalid expiry: {expiry}')


def _trade_path(filename: str) -> str:
    return os.path.join(state_mod.manual_spread_active_dir(), filename)


def _candidate_dict(c: SpreadCandidate, rank: int) -> dict:
    return {
        'rank': rank,
        'short_strike': c.short_strike,
        'long_strike': c.long_strike,
        'market_credit': c.market_credit,
        'distance_from_target': round(c.distance_from_target, 2),
        'short_mid': round(c.short_mid, 2),
        'long_mid': round(c.long_mid, 2),
        'short_symbol': c.short_symbol,
        'long_symbol': c.long_symbol,
        'overlap_warning': c.overlap_warning,
        'overlap_shifts': c.overlap_shifts,
    }


def scan_spreads(
    broker,
    *,
    side: str,
    expiry: str,
    spread_width: int,
    target_credit: float,
    max_results: int = ms_config.SCAN_MAX_RESULTS,
) -> Dict[str, Any]:
    side = side.upper()
    if side not in ('P', 'C'):
        raise ValueError('side must be P or C')
    expiry_yymmdd = parse_expiry(expiry)
    lot = 'manual-scan'
    t0 = time.time()
    entry = _manual_entry(broker)
    otm_max = resolve_scan_otm_max(
        target_credit=float(target_credit),
        default=ms_config.OTM_MAX_LOW_TARGET,
    )
    candidates = entry.scan_for_target(
        side,
        expiry_yymmdd,
        lot,
        spread_width=int(spread_width),
        target_credit=float(target_credit),
        max_results=max_results,
        otm_max=otm_max,
    )
    spx = broker.fetch_spx_price_api() or broker.get_spx_price()
    return {
        'status': 'ok',
        'spx': round(spx / 5) * 5 if spx else None,
        'target_credit': float(target_credit),
        'candidates': [_candidate_dict(c, i + 1) for i, c in enumerate(candidates)],
        'scan_ms': int((time.time() - t0) * 1000),
    }


def _resolve_strikes_for_overlap(
    expiry_yymmdd: str,
    side: str,
    short_strike: int,
    long_strike: int,
    *,
    exclude_path: str | None = None,
) -> tuple[int, int, str, str, int]:
    """Shift strikes if needed; returns (short_strike, long_strike, symbols..., shift_count)."""
    short_symbol = build_tastytrade_symbol(expiry_yymmdd, side, int(short_strike))
    long_symbol = build_tastytrade_symbol(expiry_yymmdd, side, int(long_strike))
    conflict = leg_overlap_conflict(
        short_symbol, long_symbol, side, exclude_path=exclude_path,
    )
    if not conflict:
        return int(short_strike), int(long_strike), short_symbol, long_symbol, 0

    resolved = resolve_leg_overlap(
        expiry_yymmdd,
        side,
        int(short_strike),
        int(long_strike),
        exclude_path=exclude_path,
    )
    if resolved is None:
        raise ValueError(conflict)
    ss, ls, short_symbol, long_symbol, shifts = resolved
    if shifts:
        log.info(
            'Manual %s spread shifted $5 x%d -> %s/%s to avoid leg overlap',
            side, shifts, ss, ls,
        )
    return ss, ls, short_symbol, long_symbol, shifts


def place_spread(
    broker,
    *,
    side: str,
    expiry: str,
    short_strike: int,
    long_strike: int,
    limit_credit: float,
    quantity: int,
) -> Dict[str, Any]:
    side = side.upper()
    expiry_yymmdd = parse_expiry(expiry)

    try:
        short_strike, long_strike, short_symbol, long_symbol, _shifts = _resolve_strikes_for_overlap(
            expiry_yymmdd, side, int(short_strike), int(long_strike),
        )
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}

    lot = next_lot()
    result = broker.place_spread_order(short_symbol, long_symbol, int(quantity), float(limit_credit))
    if not result.success:
        return {'status': 'error', 'error': result.message or 'Open order failed'}

    entry = _manual_entry(broker)
    path = entry.write_handshake(
        lot=lot,
        side=side,
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        short_strike=int(short_strike),
        long_strike=int(long_strike),
        quantity=int(quantity),
        open_order_id=str(result.order_id),
        limit_credit=float(limit_credit),
        strategy=ms_config.STRATEGY,
        active_directory=state_mod.manual_spread_active_dir(),
    )
    st = state_mod.load_state(path)
    register_spread_symbols(st, lot, log)
    sync_open_order(st, broker, force=True, min_interval_sec=0)
    state_mod.save_state(path, st)

    return {
        'status': 'placed',
        'filename': os.path.basename(path),
        'order_id': str(result.order_id),
        'lot': lot,
        'filled_quantity': int(st.get('filled_quantity') or 0),
    }


def modify_spread(
    broker,
    *,
    filename: str,
    new_limit_credit: float,
) -> Dict[str, Any]:
    path = _trade_path(filename)
    if not os.path.isfile(path):
        return {'status': 'error', 'error': 'trade not found'}

    state = state_mod.load_state(path)
    if state.get('status') not in ('pending_fill', 'open'):
        return {'status': 'error', 'error': f"cannot modify status={state.get('status')}"}

    filled = int(state.get('filled_quantity') or 0)
    total = int(state.get('quantity') or 0)
    remaining = total - filled
    if remaining <= 0:
        return {'status': 'error', 'error': 'order already fully filled'}

    oid = state.get('open_order_id')
    if oid:
        cancel = broker.cancel_order(str(oid))
        if not cancel.success and cancel.status not in ('cancelled', 'filled'):
            return {'status': 'error', 'error': cancel.message or 'cancel failed'}

    side = str(state.get('entry', {}).get('side', 'P')).upper()
    short_sym = state['short_leg']['symbol']
    long_sym = state['long_leg']['symbol']
    parsed = parse_canonical(short_sym)
    if not parsed:
        return {'status': 'error', 'error': f'cannot parse symbol {short_sym}'}
    expiry_yymmdd = parsed[0]
    try:
        short_strike, long_strike, short_sym, long_sym, _shifts = _resolve_strikes_for_overlap(
            expiry_yymmdd,
            side,
            int(state['short_leg']['strike']),
            int(state['long_leg']['strike']),
            exclude_path=path,
        )
    except ValueError as exc:
        return {'status': 'error', 'error': str(exc)}

    result = broker.place_spread_order(
        short_sym, long_sym, remaining, float(new_limit_credit),
    )
    if not result.success:
        return {'status': 'error', 'error': result.message or 'replace order failed'}

    state['open_order_id'] = str(result.order_id)
    state['status'] = 'pending_fill'
    state['entry']['limit_credit'] = float(new_limit_credit)
    state['entry']['net_credit'] = float(new_limit_credit)
    state['short_leg']['symbol'] = to_tastytrade(short_sym)
    state['short_leg']['strike'] = short_strike
    state['long_leg']['symbol'] = to_tastytrade(long_sym)
    state['long_leg']['strike'] = long_strike
    state['open_order'] = {
        'status': 'working',
        'last_sync': state_mod.now_iso(),
        'last_sync_epoch': 0,
        'fully_filled': False,
    }
    state_mod.save_state(path, state)
    register_spread_symbols(state, state.get('lot', 'manual'), log)
    sync_open_order(state, broker, force=True, min_interval_sec=0)
    state_mod.save_state(path, state)
    if state.get('status') == 'open' and not (state.get('active_stop') or {}).get('order_id'):
        log.info(
            'Manual spread %s filled on modify — stop_monitor will place stop on next poll',
            state.get('lot'),
        )

    return {
        'status': 'modified',
        'filename': filename,
        'order_id': str(result.order_id),
        'limit_credit': float(new_limit_credit),
    }


def cancel_spread(broker, *, filename: str) -> Dict[str, Any]:
    path = _trade_path(filename)
    if not os.path.isfile(path):
        return {'status': 'error', 'error': 'trade not found'}

    state = state_mod.load_state(path)
    oid = state.get('open_order_id')
    if oid and state.get('status') == 'pending_fill':
        broker.cancel_order(str(oid))

    state['status'] = 'cancelled'
    state_mod.save_state(path, state)

    hist_dir = state_mod.manual_spread_closed_dir()
    os.makedirs(hist_dir, exist_ok=True)
    dest = os.path.join(hist_dir, os.path.basename(path))
    shutil.move(path, dest)

    return {'status': 'cancelled', 'filename': filename}


def _load_trade_file(path: str, name: str) -> Optional[Dict[str, Any]]:
    try:
        st = state_mod.load_state(path)
        st['_filename'] = name
        st['_filepath'] = path
        return st
    except (OSError, ValueError):
        return None


def _trade_entry_date(trade: Dict[str, Any]) -> str:
    entry = trade.get('entry') or {}
    ts = entry.get('timestamp') or ''
    return ts[:10] if len(ts) >= 10 else ''


def _iter_manual_history_paths() -> List[str]:
    """JSON paths under manual spread history (flat + YYYY-MM-DD subdirs)."""
    paths: List[str] = []
    hist = state_mod.manual_spread_closed_dir()
    if not os.path.isdir(hist):
        return paths
    for name in sorted(os.listdir(hist)):
        path = os.path.join(hist, name)
        if name.endswith('.json') and os.path.isfile(path):
            paths.append(path)
            continue
        if not os.path.isdir(path) or len(name) != 10 or name[4] != '-':
            continue
        for sub in sorted(os.listdir(path)):
            if sub.endswith('.json'):
                paths.append(os.path.join(path, sub))
    return paths


def load_active_trades() -> List[Dict[str, Any]]:
    """Open/working manual spreads still in trades/active/MANUAL_SPREAD."""
    trades = []
    active = state_mod.manual_spread_active_dir()
    if not os.path.isdir(active):
        return trades
    for name in sorted(os.listdir(active)):
        if not name.endswith('.json'):
            continue
        st = _load_trade_file(os.path.join(active, name), name)
        if st:
            trades.append(st)
    return trades


def _trade_dedupe_key(trade: Dict[str, Any]) -> str:
    entry = trade.get('entry') or {}
    lot = str(trade.get('lot') or entry.get('lot') or '').strip()
    side = str(entry.get('side') or '').strip().upper()
    if not lot or not side:
        return str(trade.get('_filename') or '')
    return f'{lot}_{side}'


def _trade_recency_key(trade: Dict[str, Any]) -> tuple:
    """Sort key — later trades win dedupe."""
    entry = trade.get('entry') or {}
    ts = str(entry.get('timestamp') or '')
    path = str(trade.get('_filepath') or '')
    try:
        mtime = os.path.getmtime(path) if path and os.path.isfile(path) else 0.0
    except OSError:
        mtime = 0.0
    active_rank = 0 if str(trade.get('status') or '') in ('open', 'closing', 'pending_fill') else 1
    return (ts, mtime, str(trade.get('_filename') or ''))


def load_dashboard_manual_trades() -> List[Dict[str, Any]]:
    """
    Manual spreads for today's dashboard: active rows plus closed today from history.

    Closed trades are moved out of active/ on fill complete; keep them visible via
    history until morning archive clears prior-day expiry from active/.
    """
    from common.session_cleanup import central_today

    today = central_today().isoformat()
    by_key: Dict[str, Dict[str, Any]] = {}

    def _keep(new: Dict[str, Any], old: Optional[Dict[str, Any]]) -> bool:
        if old is None:
            return True
        new_closed = str(new.get('status') or '') == 'closed'
        old_closed = str(old.get('status') or '') == 'closed'
        if new_closed != old_closed:
            return new_closed
        return _trade_recency_key(new) >= _trade_recency_key(old)

    for st in load_active_trades():
        key = _trade_dedupe_key(st)
        if _keep(st, by_key.get(key)):
            by_key[key] = st

    for path in _iter_manual_history_paths():
        name = os.path.basename(path)
        st = _load_trade_file(path, name)
        if not st:
            continue
        if st.get('status') != 'closed':
            continue
        if _trade_entry_date(st) != today:
            continue
        key = _trade_dedupe_key(st)
        if _keep(st, by_key.get(key)):
            by_key[key] = st

    return [by_key[k] for k in sorted(by_key)]
