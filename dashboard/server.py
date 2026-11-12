"""
MEIC Dashboard Server (TastyTrade)
Run with:  python dashboard/server.py
Then open:  http://localhost:5002
"""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import common.win_ssl_env  # noqa: F401 — before any HTTPS/ssl usage

import json, subprocess, threading, time, signal, glob, logging
from datetime import date, datetime as dt, timedelta, timezone


def _central_now():
    utc = dt.now(timezone.utc)
    from common.config import _central_dst_bounds
    dst_s, dst_e = _central_dst_bounds(utc.year)
    dst_s_utc = (dst_s - timedelta(hours=-6)).replace(tzinfo=timezone.utc)
    dst_e_utc = (dst_e - timedelta(hours=-5)).replace(tzinfo=timezone.utc)
    off = -5 if dst_s_utc <= utc < dst_e_utc else -6
    return utc + timedelta(hours=off)

def _central_now_str():
    return _central_now().strftime('%H:%M:%S CT')

from typing import Optional

from flask import Flask, render_template, jsonify, request, Response
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

from streaming.config import MQTT_BROKER_ADDR

log = logging.getLogger(__name__)


def _history_money_flags() -> dict:
    """Parse history/stats money filter: live (default), paper, or all."""
    money = (request.args.get('money') or '').strip().lower()
    include_paper = request.args.get('include_paper', '').lower() in ('1', 'true', 'yes')
    paper_only = request.args.get('paper_only', '').lower() in ('1', 'true', 'yes')
    if money == 'paper':
        return {'include_paper': True, 'paper_only': True}
    if money == 'all':
        return {'include_paper': True, 'paper_only': False}
    if money == 'live':
        return {'include_paper': False, 'paper_only': False}
    return {
        'include_paper': include_paper,
        'paper_only': paper_only,
    }


def _strategy_paper_flag(name: str) -> bool:
    from common.broker_factory import is_strategy_paper
    return is_strategy_paper(name, default=True, root=ROOT)

from common import tt_config
from common.broker_factory import get_mqtt_topic_prefix
from common.meic_settings import get_meic_settings
from common.option_prices import sanitize_option_mid
sys.path.insert(0, os.path.dirname(__file__))
from db import (
    upsert_trade, get_trades, get_daily_summary, get_stats, get_stats_by_strategy,
    get_daily_breakdown, delete_trade, get_conn, STRATEGY_MEIC, STRATEGY_MANUAL,
    STRATEGY_SKYLINE, STRATEGY_PERMA_BULL, STRATEGY_MEAN_REVERSION,
    STRATEGY_IRON_FLY, STRATEGY_IRON_FLY_XSP, STRATEGY_DRMA_HM,
    STRATEGY_DRMA_HM_DIR, STRATEGY_DRMA_HM_NONDIR, STRATEGY_DRMA_HM_FUT,
    STRATEGY_DRMA_STOCKS, STRATEGY_DRMA_STOCK_DIR, STRATEGY_DRMA_STOCK_NONDIR, STRATEGY_DRMA_STOCK_EQUITY,
    STRATEGY_INVERSE_MEIC,
)
from manual_spread_handlers import (
    build_manual_trades,
    commands_dir_for_filename,
    register_manual_spread_routes,
)
from gex_handlers import register_gex_routes
from common.trade_pick import pick_best_trade
from strategies.meic.strategy import MEIC_TRANCHE_SLOTS
from blocks.stop.close_fills import display_close_prices, slippage_dollars, slippage_label
from blocks.stop.breach_watch import breach_display_fields
from dashboard.runtime_display import (
    breach_readiness_label,
    decorate_entry_label,
    is_expired_trade,
    quote_source_label,
)
from dashboard.trade_times import trade_entry_time_iso, trade_exit_time_iso
from blocks.stop.stop_math import stop_multiplier_for_state
from brokers.base import BrokerBase
from blocks.session.bootstrap import bootstrap_meic_session_if_missing
from blocks.session.manual_helpers import append_manual_session_row
from blocks.session.plan import load_meic_session_today, load_manual_session_today
from common import trades_layout
from common.session_logs import (
    LAUNCHER_BASE,
    STREAM_SCHWAB_BASE,
    STREAM_TT_BASE,
    latest_session_log,
    relocate_all_legacy_logs,
)

TOPIC_PREFIX = get_mqtt_topic_prefix() or 'TASTYTRADE/'
INDEX_TOPIC = TOPIC_PREFIX + 'SPX'

TRADES_ACTIVE_DIR = os.path.join(ROOT, tt_config.TRADES_ACTIVE_DIR)
TRADES_DIR        = os.path.join(ROOT, 'trades')
STOP_MONITOR_LOG  = os.path.join(ROOT, 'meic0dte', 'logs', 'stop_monitor.log')


def _launcher_log_path() -> str | None:
    return latest_session_log(ROOT, LAUNCHER_BASE)


def _stream_log_path() -> str | None:
    if tt_config.BROKER == 'tastytrade':
        return latest_session_log(ROOT, STREAM_TT_BASE)
    return latest_session_log(ROOT, STREAM_SCHWAB_BASE)

relocate_all_legacy_logs(ROOT)

BOT_STATUS_FILE   = os.path.join(ROOT, 'dashboard', 'bot_status.json')

TRANCHE_LOTS = [slot.lot for slot in MEIC_TRANCHE_SLOTS]
TRANCHE_SIDES = ['C', 'P']

PHASE_DISPLAY = {
    3: 'SPX Proximity Close',
    2: 'Net Credit Stop',
    1: 'Short Stop',
}

BREACH_MECHANISMS = frozenset({
    'software_breach', 'exchange_stop', 'breach',
    'phase2_upgrade', 'phase3_proximity', 'admin_3pm',
})
KILL_MECHANISMS = frozenset({'manual_close', 'admin_killswitch'})
PROFIT_TARGET_MECHANISMS = frozenset({
    'profit_target', 'profit_target_hit', 'profit_target_oco', 'profit_target_resting',
})

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'),
            static_folder=os.path.join(os.path.dirname(__file__), 'static'))

try:
    from common.trading_gate import reconcile_gate_for_session

    reconcile_gate_for_session(_central_now().strftime('%Y-%m-%d'))
except Exception:
    log.exception('Trading gate reconcile failed on dashboard startup')
app.config['SECRET_KEY'] = 'meic-dashboard-secret'
app.config['TEMPLATES_AUTO_RELOAD'] = True
socketio = SocketIO(app, cors_allowed_origins='*', async_mode='threading')

# ── Shared state ─────────────────────────────────────────────────────────────
live_prices    = {}
bot_process    = None
token_process  = None
_state_lock    = threading.Lock()
_synced_trades = set()

# Fill sync moved to dashboard/broker_fill_sync.py (lock + cooldown).

# ── Helpers ───────────────────────────────────────────────────────────────────

def read_bot_status():
    try:
        with open(BOT_STATUS_FILE) as f:
            return json.load(f)
    except Exception:
        return {'state': 'unknown', 'reason': '', 'ts': ''}


def _launcher_active() -> bool:
    """True when launcher is running (dashboard-spawned or external run.py)."""
    if bot_process is not None and bot_process.poll() is None:
        return True
    return read_bot_status().get('state') == 'running'


def read_json_safe(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def tail_log(path, n=40, buffer_size=65536):
    """Read the last n lines of path safely without reading the entire file into RAM."""
    try:
        if not os.path.isfile(path):
            return '(log not available)'
        file_size = os.path.getsize(path)
        if file_size == 0:
            return ''
        with open(path, 'rb') as f:
            if file_size <= buffer_size:
                data = f.read()
            else:
                lines_found = 0
                seek_offset = 0
                chunks = []
                while seek_offset < file_size and lines_found <= n:
                    read_size = min(buffer_size, file_size - seek_offset)
                    seek_offset += read_size
                    f.seek(file_size - seek_offset)
                    chunk = f.read(read_size)
                    chunks.append(chunk)
                    lines_found += chunk.count(b'\n')
                    if seek_offset >= file_size or lines_found > n:
                        break
                data = b''.join(reversed(chunks))
        text = data.decode('utf-8', errors='replace')
        lines = text.splitlines(keepends=True)
        return ''.join(lines[-n:])
    except Exception:
        return '(log not available)'


def _live_price(symbol: str):
    """Look up MQTT mid by symbol (handles TASTYTRADE/ prefix)."""
    if not symbol:
        return None
    for key in (TOPIC_PREFIX + symbol, symbol):
        val = live_prices.get(key)
        if val is not None:
            return sanitize_option_mid(symbol, float(val))
    return None


def _leg_mark(symbol: str, fill_price: float) -> float:
    """MQTT mid when sane; otherwise entry fill."""
    mid = _live_price(symbol)
    if mid is not None:
        return mid
    return float(fill_price or 0)


def _phase_display(
    phases: dict,
    stop_multiplier: Optional[float] = None,
    *,
    close_mechanism: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    if str(status).lower() == 'closed' and close_mechanism:
        cm = str(close_mechanism).lower()
        if cm == 'profit_target_oco':
            return 'Closed (PT 15¢)'
        if cm in ('profit_target', 'profit_target_hit', 'profit_target_resting'):
            return 'Closed (PT)'
        if cm == 'exchange_stop':
            return 'Closed (Stop)'
        if cm == 'expiry_settlement':
            return 'Settled (Expiry)'
        if cm == 'software_breach':
            return 'Closed (Breach)'
        if cm == 'manual_close':
            return 'Closed (Manual)'

    if phases.get('phase3_activated_at'):
        label = PHASE_DISPLAY[3]
    elif phases.get('phase2_activated_at'):
        label = PHASE_DISPLAY[2]
    elif phases.get('breakeven_tightened'):
        label = 'Breakeven'
    elif phases.get('phase1_active'):
        label = PHASE_DISPLAY[1]
    else:
        label = ''
    if not label or stop_multiplier is None:
        return label
    mult = float(stop_multiplier)
    if mult == int(mult):
        return f'{label} ({int(mult)}×)'
    return f'{label} ({mult:g}×)'


def _format_phase_tooltip(stop_history: Optional[list], current_label: str) -> str:
    lines = []
    for sh in (stop_history or []):
        ts = str(sh.get('timestamp') or '')
        time_str = ts[11:19] if len(ts) >= 19 else ts
        act = str(sh.get('action') or '').replace('_', ' ').title()
        reason = str(sh.get('reason') or '')
        price = sh.get('price')
        price_str = f" @ ${price:.2f}" if price is not None else ''
        oid = sh.get('order_id')
        oid_str = f" (#{oid})" if oid else ''
        detail = f"{act}{price_str}{oid_str}"
        if reason:
            detail += f" [{reason}]"
        lines.append(f"• {time_str}: {detail}" if time_str else f"• {detail}")
    if not lines:
        return f"Phase: {current_label}" if current_label else ""
    return "Phase Progression:\n" + "\n".join(lines)


def _leg_fill_label(credit, short_p, long_p) -> str:
    if short_p is None or long_p is None:
        return f'{credit:.2f}' if credit is not None else ''
    sp = float(short_p)
    lp = float(long_p)
    cr = float(credit) if credit is not None else round(sp - lp, 2)
    return f'{cr:.2f} ({sp:.2f}-{lp:.2f})'


def _slot_state_from_trade(
    status: str,
    close_mechanism: Optional[str],
    *,
    strategy: Optional[str] = None,
    trade: Optional[dict] = None,
) -> str:
    if status == 'closed':
        if is_expired_trade(trade, close_mechanism):
            return 'expired'
        if close_mechanism in KILL_MECHANISMS:
            if strategy == STRATEGY_MANUAL:
                return 'closed'
            return 'killed'
        if close_mechanism in PROFIT_TARGET_MECHANISMS or 'profit_target' in str(close_mechanism).lower():
            return 'profit_target'
        if close_mechanism in BREACH_MECHANISMS:
            return 'breached'
        return 'closed'
    if status == 'closing':
        return 'closing'
    if status == 'open':
        return 'open'
    if status == 'pending_fill':
        open_ord = (trade or {}).get('open_order') or {}
        ord_status = str(open_ord.get('status') or '').lower()
        filled_qty = int((trade or {}).get('filled_quantity') or 0)
        if ord_status in ('cancelled', 'canceled', 'rejected') and filled_qty == 0:
            return 'unfilled'
        return 'entering'
    if status in ('cancelled', 'canceled', 'rejected'):
        return 'unfilled'
    return status


def _stop_price_label(active_stop: dict) -> str:
    """Format active stop as stop/limit prices or PT/SL for OCO."""
    if not active_stop:
        return ''
    pt = active_stop.get('profit_debit') or active_stop.get('profit_price')
    sl = active_stop.get('stop_price') or active_stop.get('stop_trigger_debit')
    if pt is not None and sl is not None:
        return f'PT {float(pt):.2f} / SL {float(sl):.2f}'
    if pt is not None:
        return f'PT {float(pt):.2f}'
    sp = active_stop.get('stop_price') or active_stop.get('stop_trigger_debit')
    lp = active_stop.get('limit_price') or active_stop.get('stop_limit_debit')
    if sp is not None and lp is not None:
        return f'{float(sp):.2f}/{float(lp):.2f}'
    if sp is not None:
        return f'{float(sp):.2f}/&ndash;'
    if lp is not None:
        return f'&ndash;/{float(lp):.2f}'
    return ''


def _resolve_active_stop(trade: dict) -> dict:
    """Use working stop from JSON, or latest replaced stop in history."""
    active = dict(trade.get('active_stop') or {})
    st = str(active.get('status', '')).lower()
    if st in ('working', 'live', 'contingent', 'received', 'open', 'partially filled'):
        return active

    cancelled_oid = str(active.get('order_id') or '')
    for ev in reversed(trade.get('stop_history') or []):
        if ev.get('action') != 'placed':
            continue
        oid = str(ev.get('order_id') or '')
        if not oid or oid == cancelled_oid:
            continue
        merged = dict(active)
        merged.update({
            'order_id': oid,
            'status': 'working',
            'stop_price': ev.get('price', active.get('stop_price')),
        })
        return merged
    return active


def _trade_pnl(
    net_credit: float,
    quantity: int,
    short_fill: float,
    long_fill: float,
    short_close,
    long_close,
    cur_short: float,
    cur_long: float,
    status: str,
    *,
    trade: dict | None = None,
    spx_close: float | None = None,
) -> tuple:
    """Return (pnl, exit_credit, exit_spread_short, frozen).

    When close legs are known, PnL is frozen from fills not MQTT.
    After 3 PM CT on 0DTE expiry, open trades settle from SPX cash close.
    Includes realized PnL from any scale_outs.
    """
    scaled_pnl = 0.0
    scaled_qty = 0
    if trade and isinstance(trade, dict):
        for s in trade.get('scale_outs') or []:
            if isinstance(s, dict):
                # If scale out was split into a separate tracked row, it tracks its own PnL
                if s.get('child_lot'):
                    continue
                try:
                    scaled_qty += int(s.get('quantity', 0) or 0)
                    if s.get('pnl') is not None:
                        scaled_pnl += float(s['pnl'])
                except (ValueError, TypeError):
                    pass

    # Determine effective quantity for the held / remaining units
    if status == 'closed':
        c = trade.get('close') if isinstance(trade, dict) else None
        if isinstance(c, dict) and c.get('quantity') is not None and int(c['quantity']) > 0:
            active_qty = int(c['quantity'])
        else:
            active_qty = quantity if quantity > 0 else (int(trade.get('initial_quantity') or 1) if isinstance(trade, dict) else 1)
    else:
        active_qty = int(trade.get('holding_quantity') or quantity) if isinstance(trade, dict) else quantity

    sc = float(short_close) if short_close is not None else None
    lc = float(long_close) if long_close is not None else None

    # Check close block if sc / lc are missing
    if (sc is None or lc is None) and trade and isinstance(trade, dict):
        c = trade.get('close')
        if isinstance(c, dict):
            if sc is None and c.get('short_close_price') is not None:
                try:
                    sc = float(c['short_close_price'])
                except (ValueError, TypeError):
                    pass
            if lc is None and c.get('long_close_price') is not None:
                try:
                    lc = float(c['long_close_price'])
                except (ValueError, TypeError):
                    pass
        if sc is None and isinstance(trade, dict):
            for ev in reversed(trade.get('stop_history') or []):
                if ev.get('action') == 'replaced_limit' and ev.get('price'):
                    try:
                        sc = float(ev['price'])
                        break
                    except (ValueError, TypeError):
                        pass

    is_debit = bool(
        isinstance(trade, dict)
        and (
            str(trade.get('spread_type') or (trade.get('entry') or {}).get('spread_type') or '').lower() == 'debit'
            or str(trade.get('side') or (trade.get('entry') or {}).get('side') or '').upper() in ('PDS', 'CDS')
            or str(trade.get('structure_type') or (trade.get('entry') or {}).get('structure_type') or '').lower() in ('debit_p', 'debit_c', 'pds', 'cds')
        )
    )

    if status == 'closed' and sc is not None and lc is not None:
        if is_debit:
            exit_credit = round(lc - sc, 2)
            pnl = round((exit_credit - net_credit) * 100 * active_qty + scaled_pnl, 2)
        else:
            exit_credit = round(sc - lc, 2)
            pnl = round((net_credit - exit_credit) * 100 * active_qty + scaled_pnl, 2)
        return pnl, exit_credit, sc, True

    if status == 'closed' and trade and isinstance(trade, dict):
        c = trade.get('close')
        ec = None
        if isinstance(c, dict) and c.get('close_price') is not None:
            ec = float(c['close_price'])
        elif trade.get('exit_credit') is not None:
            ec = float(trade['exit_credit'])
        elif trade.get('close_debit') is not None:
            ec = float(trade['close_debit'])
        if ec is not None:
            if is_debit:
                pnl = round((ec - net_credit) * 100 * active_qty + scaled_pnl, 2)
            else:
                pnl = round((net_credit - ec) * 100 * active_qty + scaled_pnl, 2)
            return pnl, ec, sc, True

    if trade is not None and spx_close is not None:
        from common.expiry_settlement import compute_settled_pnl
        settled = compute_settled_pnl(trade, spx_close)
        if settled is not None and settled.get('settled'):
            exit_credit = settled['close_debit']
            pnl = round(float(settled['pnl']) + scaled_pnl, 2)
            return pnl, exit_credit, settled['short_close_price'], True

    if sc is not None:
        long_for_exit = lc if lc is not None else (cur_long if cur_long is not None else (long_fill or 0.0))
        if is_debit:
            exit_credit = round(long_for_exit - sc, 2)
            pnl = round((exit_credit - net_credit) * 100 * active_qty + scaled_pnl, 2)
        else:
            exit_credit = round(sc - long_for_exit, 2)
            pnl = round((net_credit - exit_credit) * 100 * active_qty + scaled_pnl, 2)
        return pnl, exit_credit, sc, True

    cur_s = cur_short if cur_short is not None else (short_fill or 0.0)
    cur_l = cur_long if cur_long is not None else (long_fill or 0.0)
    if is_debit:
        cur_spread = round(cur_l - cur_s, 2)
        pnl = round((cur_spread - net_credit) * 100 * active_qty + scaled_pnl, 2)
    else:
        cur_spread = round(cur_s - cur_l, 2)
        pnl = round((net_credit - cur_spread) * 100 * active_qty + scaled_pnl, 2)
    return pnl, None, None, False


def _read_active_trades():
    """Read all per-trade JSON from trades/active/{strategy}/."""
    trades = []
    try:
        from blocks.stop import state as state_mod

        for fpath in state_mod.iter_active_trade_paths():
            state = read_json_safe(fpath)
            if state and isinstance(state, dict):
                state['_filepath'] = fpath
                state['_filename'] = os.path.basename(fpath)
                trades.append(state)
    except Exception:
        pass
    return trades



def _match_trade_to_slot(trade, lot, side, for_date=None):
    """Check if a trade JSON belongs to a given lot+side slot."""
    entry = trade.get('entry', {})
    trade_lot = trade.get('lot', '') or entry.get('lot', '')
    trade_side = entry.get('side', '')
    if trade_lot != lot or trade_side != side:
        return False
    if for_date is not None:
        from common.session_cleanup import trade_expiry_date
        exp = trade_expiry_date(trade, trade.get('_filename', ''))
        entry_ts = str(entry.get('timestamp') or trade.get('date') or '')
        entry_d = None
        if len(entry_ts) >= 10:
            try:
                entry_d = date.fromisoformat(entry_ts[:10])
            except ValueError:
                pass
        target_d = date.fromisoformat(for_date) if isinstance(for_date, str) else for_date
        if exp is not None and exp < target_d:
            return False
        if entry_d is not None and entry_d < target_d:
            return False
    return True


_RESOLVED_TRADE_PATHS: dict[str, str] = {}


def _resolve_trade_path(trade_path: str) -> str:
    """Resolve session CSV trade_path — active file or archived copy under history/."""
    if not trade_path:
        return ''
    if os.path.isfile(trade_path):
        return trade_path
    cached = _RESOLVED_TRADE_PATHS.get(trade_path)
    if cached and os.path.isfile(cached):
        return cached
    base = os.path.basename(trade_path)
    from common import trades_layout
    for rel in (trades_layout.MEIC_HISTORY, trades_layout.MANUAL_HISTORY):
        hist_root = os.path.join(ROOT, rel)
        if not os.path.isdir(hist_root):
            continue
        direct = os.path.join(hist_root, base)
        if os.path.isfile(direct):
            _RESOLVED_TRADE_PATHS[trade_path] = direct
            return direct
        for day_dir in sorted(glob.glob(os.path.join(hist_root, '*')), reverse=True):
            candidate = os.path.join(day_dir, base)
            if os.path.isfile(candidate):
                _RESOLVED_TRADE_PATHS[trade_path] = candidate
                return candidate
    return trade_path


MEIC_GRID_PNL_STATES = frozenset({
    'open', 'closing', 'breached', 'working', 'closed', 'killed', 'expired', 'profit_target',
})


def _trade_has_position(trade: dict) -> bool:
    """True when the trade JSON represents a real filled position."""
    from common.expiry_settlement import effective_filled_quantity, has_real_close_fills

    status = (trade.get('status') or '').lower()
    if has_real_close_fills(trade):
        return True
    filled = effective_filled_quantity(trade)
    if filled > 0:
        return True
    if status in ('pending_fill', 'cancelled', 'canceled', 'rejected'):
        return False
    short_fill = float((trade.get('short_leg') or {}).get('fill_price') or 0)
    long_fill = float((trade.get('long_leg') or {}).get('fill_price') or 0)
    return short_fill > 0 and long_fill > 0


def _counts_toward_meic_day_pnl(row, trade, display_state: str) -> bool:
    """Match MEIC tranche grid totals: real fills in grid-visible states only."""
    if trade is None or not _trade_has_position(trade):
        return False
    if display_state not in MEIC_GRID_PNL_STATES:
        return False
    if getattr(row, 'state', '') in ('failed', 'unfilled') and not getattr(row, 'trade_path', ''):
        return False
    return True


def _resolve_trade_for_row(row, active_trades, for_date=None):
    if row.trade_path:
        resolved = _resolve_trade_path(row.trade_path)
        if resolved and os.path.isfile(resolved):
            state = read_json_safe(resolved)
            if state:
                state['_filepath'] = resolved
                state['_filename'] = os.path.basename(resolved)
                return state
    if getattr(row, 'state', '') in ('failed', 'unfilled'):
        return None
    matching = [t for t in active_trades if _match_trade_to_slot(t, row.lot, row.side, for_date=for_date)]
    return pick_best_trade(matching)


def _session_display_state(row, trade) -> str:
    if row.skip:
        return 'skipped'
    if row.paused and row.state in ('pending', 'entering'):
        return 'paused'
    if trade:
        trade_status = trade.get('status', 'unknown')
        entry = trade.get('entry') or {}
        if row.state == 'entering' and trade_status in ('open', 'closing', 'pending_fill'):
            return _slot_state_from_trade(
                trade_status,
                trade.get('close_mechanism'),
                strategy=entry.get('strategy'),
                trade=trade,
            )
    if row.state == 'entering':
        return 'entering'
    if row.state == 'waiting_rest_probe':
        return 'waiting_rest_probe'
    if row.state == 'blocked_rest':
        return 'blocked_rest'
    if row.state in ('failed', 'unfilled'):
        return row.state
    if trade:
        entry = trade.get('entry') or {}
        return _slot_state_from_trade(
            trade.get('status', 'unknown'),
            trade.get('close_mechanism'),
            strategy=entry.get('strategy'),
            trade=trade,
        )
    if row.state == 'entered':
        return 'entered'
    return row.state or 'pending'


def _apply_trade_overlay(slot, trade, lot, side, spx_settle=None):
    status = trade.get('status', 'unknown')
    entry = trade.get('entry', {})
    short_leg = trade.get('short_leg', {})
    long_leg = trade.get('long_leg', {})
    phases = trade.get('phases', {})

    short_sym = short_leg.get('symbol', '')
    long_sym = long_leg.get('symbol', '')
    net_credit = float(entry.get('net_credit', 0) or 0)
    has_pos = _trade_has_position(trade)
    quantity = int(trade.get('filled_quantity') or trade.get('quantity') or 1) if has_pos else int(trade.get('filled_quantity', 0) or 0)
    short_fill = float(short_leg.get('fill_price') or 0)
    long_fill = float(long_leg.get('fill_price') or 0)
    short_close, long_close = display_close_prices(trade)

    cur_long = _leg_mark(long_sym, long_fill)
    cur_short = _leg_mark(short_sym, short_fill)
    watch = trade.get('breach_watch') or {}
    if (
        status in ('open', 'closing')
        and watch.get('spread_mid') is not None
        and watch.get('short_mqtt')
        and watch.get('long_mqtt')
    ):
        cur_short = round(cur_long + float(watch['spread_mid']), 4)
    cur_spread = round(cur_short - cur_long, 2)

    breach_fields = breach_display_fields(
        trade,
        live_short=_live_price(short_sym),
        live_long=_live_price(long_sym),
        trade_status=status,
    )

    close_mechanism = trade.get('close_mechanism')
    trade_spx = spx_settle
    if trade:
        from common.session_cleanup import trade_expiry_date, central_today
        exp = trade_expiry_date(trade, trade.get('_filename', ''))
        if exp is not None and (trade_spx is None or exp != central_today()):
            from common.expiry_settlement import get_spx_settlement_close, ensure_spx_settlement_close
            trade_spx = get_spx_settlement_close(exp, root=ROOT) or ensure_spx_settlement_close(exp, root=ROOT)

    if has_pos:
        live_pnl, exit_credit, exit_short, pnl_frozen = _trade_pnl(
            net_credit, quantity, short_fill, long_fill,
            short_close, long_close, cur_short, cur_long, status,
            trade=trade, spx_close=trade_spx,
        )
    else:
        live_pnl, exit_credit, exit_short, pnl_frozen = None, None, None, True

    phase_label = _phase_display(
        phases,
        stop_multiplier_for_state(trade),
        close_mechanism=close_mechanism,
        status=status,
    )
    phase_tooltip = _format_phase_tooltip(trade.get('stop_history', []), phase_label)
    active_stop = _resolve_active_stop(trade)
    stop_order_id = active_stop.get('order_id') or ''

    entry_label = decorate_entry_label(
        trade,
        _leg_fill_label(net_credit, short_fill, long_fill),
    )
    exit_label = ''
    if exit_short is not None:
        le = float(long_close) if long_close is not None else float(cur_long)
        asterisk = '*' if long_close is None and status == 'closing' else ''
        exit_label = (
            f'{exit_credit:.2f} ({float(exit_short):.2f}-{le:.2f}{asterisk})'
        )

    slip_sp = None
    slip_usd = None
    if status == 'closed' or short_close is not None:
        slip_usd = slippage_dollars(trade)

    slot.update({
        'short_strike': short_leg.get('strike', ''),
        'long_strike': long_leg.get('strike', ''),
        'time_opened': entry.get('timestamp', ''),
        'entry_time': trade_entry_time_iso(trade),
        'exit_time': trade_exit_time_iso(trade),
        'entry_credit': net_credit,
        'entry_label': entry_label,
        'exit_label': exit_label,
        'slippage': slip_usd,
        'slippage_label': slippage_label(slip_usd),
        'slippage_dollars': slip_usd,
        'designated_stop_price': trade.get('designated_stop_price'),
        'quantity': quantity,
        'initial_quantity': trade.get('initial_quantity') or quantity,
        'scale_outs': trade.get('scale_outs') or [],
        'scaled_qty': sum(int(s.get('quantity', 0) or 0) for s in (trade.get('scale_outs') or [])),
        'cur_short': cur_short if not pnl_frozen else (exit_short or cur_short),
        'cur_long': cur_long if not pnl_frozen else (
            float(long_close) if long_close is not None else cur_long
        ),
        'cur_spread': (
            round(exit_credit, 2) if exit_credit is not None and pnl_frozen
            else cur_spread
        ),
        'live_pnl': live_pnl,
        'pnl_frozen': pnl_frozen,
        'pnl_class': ('text-success' if live_pnl >= 0 else 'text-danger') if live_pnl is not None else '',
        'phase': phase_label,
        'phase_tooltip': phase_tooltip,
        'stop_history': trade.get('stop_history', []),
        'stop_order_id': stop_order_id,
        'stop_price': active_stop.get('stop_price'),
        'limit_price': active_stop.get('limit_price'),
        'stop_label': _stop_price_label(active_stop),
        'close_mechanism': close_mechanism or '',
        'entry_quote_source_label': quote_source_label(trade),
        'breach_readiness_label': breach_readiness_label(trade),
        '_filename': trade.get('_filename', ''),
        **breach_fields,
    })
    return status, live_pnl, entry, short_sym, long_sym, short_leg, long_leg, quantity, net_credit, trade


def _meic_session_rows():
    bootstrap_meic_session_if_missing(ROOT)
    return load_meic_session_today(ROOT)


def _patch_session_rows(slot_keys, action, **fields):
    plan = _meic_session_rows()
    if plan is None:
        return None
    for slot_key in slot_keys:
        row = plan.row_by_slot_key(slot_key)
        if row is None:
            continue
        if action == 'pause':
            plan.update_row(slot_key, paused=True)
        elif action == 'unpause':
            plan.update_row(slot_key, paused=False)
        else:
            plan.update_row(slot_key, **fields)
    plan.save()
    return plan


def _skyline_today_pnl() -> float:
    """Today's Skyline paper P/L (realized if closed, else unrealized)."""
    from common.session_cleanup import central_today
    from strategies.skyline.settlement import attach_dollar_pnl_display

    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, ROOT)
    trade = None
    for candidate in (
        os.path.join(active, f'skyline_{today}.json'),
        os.path.join(hist_dir, f'skyline_{today}.json'),
    ):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding='utf-8') as f:
                trade = json.load(f)
            break
        except (OSError, json.JSONDecodeError):
            trade = None
    if not trade:
        return 0.0
    attach_dollar_pnl_display(trade)
    if trade.get('status') == 'closed':
        dollars = trade.get('realized_pnl_dollars')
        if dollars is not None:
            return float(dollars)
        raw = trade.get('realized_pnl_total')
        return float(raw) * 100 if raw is not None else 0.0
    dollars = trade.get('unrealized_pnl_dollars')
    if dollars is not None:
        return float(dollars)
    raw = trade.get('unrealized_pnl_total')
    return float(raw) * 100 if raw is not None else 0.0


def _perma_bull_trade_for_date(for_date: str | None = None) -> dict | None:
    """Load today's (or given date) Perma Bull PCS handshake trade from disk."""
    from common.session_cleanup import central_today

    today = for_date or central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_PERMA_BULL, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_PERMA_BULL, ROOT)
    lot_hint = f'perma-{today}'
    for root_dir in (active, hist_dir):
        if not os.path.isdir(root_dir):
            continue
        for path in sorted(glob.glob(os.path.join(root_dir, '*.json')), reverse=True):
            base = os.path.basename(path)
            if base.startswith(('session_', 'halted_', 'skyline_')):
                continue
            trade = read_json_safe(path)
            if not trade or not isinstance(trade, dict):
                continue
            entry = trade.get('entry') or {}
            lot = str(trade.get('lot') or entry.get('lot') or '')
            entry_date = str(entry.get('timestamp') or '')[:10]
            if lot == lot_hint or entry_date == today:
                trade['_filepath'] = path
                return trade
    return None


def _handshake_trade_live_pnl(trade: dict, spx_settle: float | None = None) -> float:
    """P/L dollars for a vertical-spread handshake JSON."""
    entry = trade.get('entry') or {}
    short_leg = trade.get('short_leg') or {}
    long_leg = trade.get('long_leg') or {}
    status = str(trade.get('status') or '').lower()
    quantity = int(trade.get('quantity') or trade.get('filled_quantity') or 0)
    net_credit = float(entry.get('net_credit') or entry.get('limit_credit') or 0)
    short_fill = float(short_leg.get('fill_price') or 0)
    long_fill = float(long_leg.get('fill_price') or 0)
    short_sym = short_leg.get('symbol') or ''
    long_sym = long_leg.get('symbol') or ''
    short_close = trade.get('short_close_price')
    long_close = trade.get('long_close_price')
    cur_short = _live_price(short_sym) if short_sym else 0.0
    cur_long = _live_price(long_sym) if long_sym else 0.0
    live_pnl, _, _, _ = _trade_pnl(
        net_credit,
        quantity,
        short_fill,
        long_fill,
        short_close,
        long_close,
        cur_short,
        cur_long,
        status,
        trade=trade,
        spx_close=spx_settle,
    )
    return float(live_pnl)


def _perma_bull_today_pnl(spx_settle: float | None = None) -> float:
    trade = _perma_bull_trade_for_date()
    if not trade:
        return 0.0
    return _handshake_trade_live_pnl(trade, spx_settle=spx_settle)


def _perma_bull_history_rows(limit: int = 30) -> list:
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_PERMA_BULL, ROOT)
    rows = []
    if not os.path.isdir(hist_dir):
        return rows
    for path in sorted(glob.glob(os.path.join(hist_dir, '*.json')), reverse=True):
        base = os.path.basename(path)
        if base.startswith(('session_', 'halted_')):
            continue
        trade = read_json_safe(path)
        if not trade or trade.get('status') != 'closed':
            continue
        entry = trade.get('entry') or {}
        short_leg = trade.get('short_leg') or {}
        long_leg = trade.get('long_leg') or {}
        close_info = trade.get('close') or {}
        pnl = _handshake_trade_live_pnl(trade)
        close_debit = trade.get('brokerage_spread_exit_debit')
        if close_debit is None:
            sc = close_info.get('short_close_price') or trade.get('short_close_price')
            lc = close_info.get('long_close_price') or trade.get('long_close_price')
            if sc is not None and lc is not None:
                close_debit = round(float(sc) - float(lc), 2)
            elif sc is not None:
                close_debit = float(sc)
        rows.append({
            'date': str(entry.get('timestamp') or '')[:10],
            'entry_time': trade_entry_time_iso(trade),
            'exit_time': trade_exit_time_iso(trade),
            'status': trade.get('status'),
            'quantity': int(trade.get('quantity') or trade.get('filled_quantity') or 1),
            'short_strike': short_leg.get('strike'),
            'long_strike': long_leg.get('strike'),
            'net_credit': entry.get('net_credit'),
            'close_debit': close_debit,
            'close_reason': close_info.get('reason') or trade.get('close_mechanism'),
            'realized_pnl_dollars': pnl,
            'paper': bool(trade.get('paper')),
        })
        if len(rows) >= limit:
            break
    return rows


def _iron_fly_today_pnl() -> float:
    """Today's Iron Fly paper/live P/L (realized + open)."""
    from common.session_cleanup import central_today
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY, ROOT)
    for candidate in (
        os.path.join(active, f'iron_fly_{today}.json'),
        os.path.join(hist_dir, f'iron_fly_{today}.json'),
    ):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding='utf-8') as f:
                book = json.load(f)
            realized = float(book.get('realized_pnl', 0.0))
            open_pnl = float(book.get('open_pnl', 0.0))
            return realized + open_pnl
        except (OSError, json.JSONDecodeError):
            pass
    return 0.0


def _iron_fly_xsp_today_pnl() -> float:
    """Today's XSP Iron Fly paper/live P/L (realized + open)."""
    from common.session_cleanup import central_today
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, ROOT)
    for candidate in (
        os.path.join(active, f'iron_fly_xsp_{today}.json'),
        os.path.join(hist_dir, f'iron_fly_xsp_{today}.json'),
    ):
        if not os.path.isfile(candidate):
            continue
        try:
            with open(candidate, encoding='utf-8') as f:
                book = json.load(f)
            realized = float(book.get('realized_pnl', 0.0))
            open_pnl = float(book.get('open_pnl', 0.0))
            return realized + open_pnl
        except (OSError, json.JSONDecodeError):
            pass
    return 0.0


def _collect_drma_hm_session_trade_files(directory: str, today_str: str) -> list[str]:
    """Collect DRMA_HM trade JSON files for today's trading session, including overnight futures trades."""
    if not os.path.isdir(directory):
        return []
    import glob
    import datetime
    from common.session_cleanup import central_today

    today_dt = central_today()
    search_dates = [today_str] + [(today_dt - datetime.timedelta(days=d)).isoformat() for d in (1, 2, 3)]

    matched_paths = []
    seen_ids = set()

    for p_date in search_dates:
        for path in sorted(glob.glob(os.path.join(directory, f'drma_hm_{p_date}_*.json')), reverse=True):
            fname = os.path.basename(path)
            if fname.startswith('session_'):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    t = json.load(f)
            except Exception:
                continue

            tid = t.get('trade_id') or fname
            if tid in seen_ids:
                continue

            entered_at = str(t.get('entered_at') or '')
            is_today = (p_date == today_str) or (t.get('date') == today_str)
            is_overnight = (
                p_date != today_str and
                (t.get('session_phase') == 'overnight' or
                 t.get('structure_type') == 'futures' or
                 t.get('trade_type') in ('DIRECTIONAL_FUTURES', 'FUTURES')) and
                (entered_at >= f'{p_date}T17:00:00')
            )

            if is_today or is_overnight:
                seen_ids.add(tid)
                matched_paths.append(path)

    return matched_paths


def _drma_hm_today_pnl() -> float:
    """Today's DRMA_HM paper/live P/L (realized + open)."""
    from common.session_cleanup import central_today
    from strategies.drma_hm.settlement import attach_dollar_pnl_display

    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)

    total = 0.0
    for d in (active, hist_dir):
        for path in _collect_drma_hm_session_trade_files(d, today):
            try:
                with open(path, encoding='utf-8') as f:
                    trade = json.load(f)
                attach_dollar_pnl_display(trade)
                if trade.get('status') == 'closed':
                    total += float(trade.get('realized_pnl_dollars', 0.0))
                else:
                    total += float(trade.get('unrealized_pnl_dollars', 0.0))
            except (OSError, json.JSONDecodeError):
                pass
    return round(total, 2)


def _drma_stocks_today_pnl() -> float:
    """Today's DRMA_HM_STOCKS paper P/L (realized + open)."""
    import glob
    from common.session_cleanup import central_today
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)

    total = 0.0
    for d in (active, hist_dir):
        if not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, '*.json')):
            if os.path.basename(path).startswith('session_'):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    trade = json.load(f)
                if trade.get('status') == 'closed':
                    closed_at = trade.get('closed_at') or trade.get('entered_at') or ''
                    if not closed_at.startswith(today):
                        continue
                    total += float(trade.get('realized_pnl_dollars', 0.0))
                else:
                    total += float(trade.get('unrealized_pnl_dollars', 0.0))
            except (OSError, json.JSONDecodeError):
                pass
    return round(total, 2)


def _resolve_strategy_config(name: str):
    lookup_name = 'DRMA_HM_SPX' if name in ('DRMA_HM_DIR', 'DRMA_HM_NONDIR', 'DRMA_HM_FUT') else name
    if name in ('DRMA_STOCK_DIR', 'DRMA_STOCK_NONDIR', 'DRMA_STOCK_EQUITY'):
        lookup_name = 'DRMA_HM_STOCKS'
    from strategies.loader import load_strategies
    for strat, cfg in load_strategies(include_disabled=True):
        if cfg.name == lookup_name:
            return strat, cfg, type(cfg)
    if name in ('DRMA_HM_SPX', 'DRMA_HM_DIR', 'DRMA_HM_NONDIR', 'DRMA_HM_FUT'):
        try:
            from strategies.drma_hm.config import DRMAHMConfig
            from strategies.drma_hm.strategy import DRMAHMStrategy
            return DRMAHMStrategy(), DRMAHMConfig(), DRMAHMConfig
        except Exception:
            pass
    if name in ('DRMA_HM_STOCKS', 'DRMA_STOCK_DIR', 'DRMA_STOCK_NONDIR', 'DRMA_STOCK_EQUITY'):
        try:
            from strategies.drma_stocks.config import DRMAStocksConfig
            from strategies.drma_stocks.strategy import DRMAStocksStrategy
            return DRMAStocksStrategy(), DRMAStocksConfig(), DRMAStocksConfig
        except Exception:
            pass
    if name == 'MANUAL_SPREAD':
        try:
            from strategies.manual_spread.strategy import ManualSpreadStrategy, ManualSpreadConfig
            return ManualSpreadStrategy(), ManualSpreadConfig(name='MANUAL_SPREAD'), ManualSpreadConfig
        except Exception:
            pass
    return None, None, None


ALL_STRATEGY_META = [
    {'name': 'MEIC_IC', 'label': 'MEIC Iron Condor', 'tab_id': 'meic', 'badge_id': 'meic-nav-badge'},
    {'name': 'MANUAL_SPREAD', 'label': 'Manual Spread', 'tab_id': 'manual', 'badge_id': 'manual-nav-badge'},
    {'name': 'CLONE_TRADES', 'label': 'Discretionary Trades', 'tab_id': 'clone_trades', 'badge_id': 'clone-nav-badge'},
    {'name': 'SKYLINE_SPX', 'label': 'Skyline SPX', 'tab_id': 'skyline', 'badge_id': 'skyline-nav-badge'},
    {'name': 'PERMA_BULL_PCS', 'label': 'Perma Bull PCS', 'tab_id': 'perma-bull', 'badge_id': 'perma-bull-nav-badge'},
    {'name': 'MEAN_REVERSION_SPX', 'label': 'Mean Reversion SPX', 'tab_id': 'mean_reversion', 'badge_id': 'mean-reversion-nav-badge'},
    {'name': 'IRON_FLY_SPX', 'label': 'Iron Fly SPX', 'tab_id': 'iron_fly', 'badge_id': 'iron-fly-nav-badge'},
    {'name': 'IRON_FLY_XSP', 'label': 'Iron Fly XSP', 'tab_id': 'iron_fly_xsp', 'badge_id': 'iron-fly-xsp-nav-badge'},
    {'name': 'DRMA_HM_SPX', 'label': 'DRMA Hilega Milega Pro', 'tab_id': 'drma_hm', 'badge_id': 'drma-hm-nav-badge'},
    {'name': 'DRMA_HM_STOCKS', 'label': 'DRMA Stocks Pro', 'tab_id': 'drma_stocks', 'badge_id': 'drma-stocks-nav-badge'},
]


def _is_strategy_enabled(name: str) -> bool:
    """Return whether a strategy is active / enabled for today."""
    if name in ('MEIC_IC', 'MEIC'):
        try:
            from common.daily_overrides import get_overrides
            ov = get_overrides('MEIC_IC', ROOT)
            if 'enabled' in ov:
                return bool(ov['enabled'])
            from blocks.session.plan import load_meic_session_today
            plan = load_meic_session_today(ROOT)
            if plan and plan.rows:
                return any(not r.paused and not r.skip for r in plan.rows)
            from common.meic_settings import get_meic_settings
            st = get_meic_settings(root=ROOT)
            return bool(st.side_ccs_enabled or st.side_pcs_enabled)
        except Exception:
            return True

    if name == 'CLONE_TRADES':
        try:
            import json
            cfg_p = os.path.join(ROOT, 'trades', 'clone_trades', 'config.json')
            if os.path.exists(cfg_p):
                with open(cfg_p, 'r', encoding='utf-8') as f:
                    return bool(json.load(f).get('auto_take_trades', True))
        except Exception:
            return True
        return True

    try:
        from common.daily_overrides import get_overrides
        ov = get_overrides(name, ROOT)
        if 'enabled' in ov:
            return bool(ov['enabled'])
    except Exception:
        pass

    try:
        from strategies.loader import load_strategies_yaml_raw
        for s in load_strategies_yaml_raw():
            if s.get('name') == name:
                return bool(s.get('enabled', True))
    except Exception:
        pass

    return True


def _get_strategy_control_status(name: str) -> dict:
    meta = next((m for m in ALL_STRATEGY_META if m['name'] == name), None)
    if not meta:
        meta = {'name': name, 'label': name, 'tab_id': name.lower(), 'badge_id': f'{name.lower()}-nav-badge'}
    return {
        'name': meta['name'],
        'label': meta['label'],
        'tab_id': meta['tab_id'],
        'badge_id': meta['badge_id'],
        'enabled': _is_strategy_enabled(meta['name']),
        'paper': _strategy_paper_flag(meta['name']),
    }


def _get_all_strategy_controls() -> dict:
    return {m['name']: _get_strategy_control_status(m['name']) for m in ALL_STRATEGY_META}


def _set_strategy_control(name: str, *, enabled: Optional[bool] = None, paper: Optional[bool] = None) -> dict:
    if name in ('MEIC_IC', 'MEIC'):
        from common.meic_settings import get_meic_settings, save_meic_settings
        st = get_meic_settings(root=ROOT)
        if paper is not None:
            st.paper_mode = bool(paper)
            try:
                from common.daily_overrides import set_overrides
                from strategies.meic.strategy import MEICConfig
                set_overrides('MEIC_IC', ROOT, {'paper': bool(paper)}, MEICConfig)
            except Exception:
                pass
        if enabled is not None:
            st.side_ccs_enabled = bool(enabled)
            st.side_pcs_enabled = bool(enabled)
            try:
                from blocks.session.bootstrap import bootstrap_meic_session_if_missing
                from blocks.session.plan import load_meic_session_today
                bootstrap_meic_session_if_missing(ROOT)
                plan = load_meic_session_today(ROOT)
                if plan:
                    for row in plan.rows:
                        plan.update_row(row.slot_key, paused=not bool(enabled))
                    plan.save()
            except Exception:
                pass
            try:
                from common.daily_overrides import set_overrides
                from strategies.meic.strategy import MEICConfig
                set_overrides('MEIC_IC', ROOT, {'enabled': bool(enabled)}, MEICConfig)
            except Exception:
                pass
        save_meic_settings(st, root=ROOT)

    elif name == 'CLONE_TRADES':
        cfg_p = os.path.join(ROOT, 'trades', 'clone_trades', 'config.json')
        cfg_data = {}
        if os.path.exists(cfg_p):
            try:
                with open(cfg_p, 'r', encoding='utf-8') as f:
                    cfg_data = json.load(f)
            except Exception:
                pass
        if enabled is not None:
            cfg_data['auto_take_trades'] = bool(enabled)
        if paper is not None:
            cfg_data['paper_mode'] = bool(paper)
        os.makedirs(os.path.dirname(cfg_p), exist_ok=True)
        tmp = f'{cfg_p}.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg_data, f, indent=2)
            f.flush()
        os.replace(tmp, cfg_p)

    elif name == 'MANUAL_SPREAD':
        from common.daily_overrides import set_overrides
        from strategies.manual_spread.strategy import ManualSpreadConfig
        updates = {}
        if enabled is not None:
            updates['enabled'] = bool(enabled)
        if paper is not None:
            updates['paper'] = bool(paper)
        if updates:
            set_overrides('MANUAL_SPREAD', ROOT, updates, ManualSpreadConfig)

    elif name in ('DRMA_HM_DIR', 'DRMA_HM_NONDIR', 'DRMA_HM_FUT'):
        from common.daily_overrides import set_overrides
        from strategies.drma_hm.config import DRMAHMConfig
        updates = {}
        if paper is not None:
            if name == 'DRMA_HM_DIR':
                updates['dir_paper'] = bool(paper)
            elif name == 'DRMA_HM_NONDIR':
                updates['non_dir_paper'] = bool(paper)
            elif name == 'DRMA_HM_FUT':
                updates['futures_paper'] = bool(paper)
        if updates:
            set_overrides('DRMA_HM_SPX', ROOT, updates, DRMAHMConfig)

    else:
        # Standalone strategies: SKYLINE_SPX, PERMA_BULL_PCS, MEAN_REVERSION_SPX, IRON_FLY_SPX, IRON_FLY_XSP, DRMA_HM_SPX
        _, _, cfg_cls = _resolve_strategy_config(name)
        if cfg_cls is not None:
            from common.daily_overrides import set_overrides
            updates = {}
            if enabled is not None:
                updates['enabled'] = bool(enabled)
            if paper is not None:
                updates['paper'] = bool(paper)
                if name in ('IRON_FLY_SPX', 'IRON_FLY_XSP'):
                    updates['mode'] = 'PAPER' if paper else 'LIVE_AUTO'
                elif name == 'DRMA_HM_SPX':
                    updates['dir_paper'] = bool(paper)
                    updates['non_dir_paper'] = bool(paper)
                    updates['futures_paper'] = bool(paper)
            if updates:
                set_overrides(name, ROOT, updates, cfg_cls)

    if paper is not None:
        try:
            from common.strategy_modes import set_strategy_paper_mode
            set_strategy_paper_mode(name, paper=bool(paper), root=ROOT)
        except Exception as exc:
            log.warning('Could not persist strategy mode for %s: %s', name, exc)

    emit_summary_update()
    return _get_strategy_control_status(name)


def build_summary():
    from common.expiry_settlement import ensure_spx_settlement_close, get_spx_settlement_close, settlement_cutoff_reached
    from common.session_cleanup import central_today

    today = central_today()

    # Pre-market auto-cleanup: if active trades exist whose expiry < today, settle and archive them
    try:
        from common.session_cleanup import settle_expired_active_trades, archive_active_trades
        meic_act = os.path.join(ROOT, tt_config.TRADES_ACTIVE_DIR)
        meic_hist = os.path.join(ROOT, tt_config.TRADES_CLOSED_DIR)
        ms_settled, _ = settle_expired_active_trades(meic_act, ROOT, today)
        if ms_settled > 0:
            archive_active_trades(meic_act, meic_hist, today, 'morning')
            active_trades = _read_active_trades()
    except Exception as exc:
        log.warning('Pre-market auto-cleanup failed: %s', exc)

    active_trades = _read_active_trades()
    bot_status = read_bot_status()
    session_plan = _meic_session_rows()

    spx_settle = get_spx_settlement_close(today, root=ROOT)
    if spx_settle is None and settlement_cutoff_reached(today):
        spx_settle = ensure_spx_settlement_close(today, root=ROOT)

    grid = []
    day_pnl = 0.0
    day_slippage = 0.0
    open_count = 0
    closed_count = 0

    if session_plan:
        row_iter = session_plan.rows
    else:
        row_iter = [
            type('R', (), {'lot': lot, 'side': side, 'slot_key': f'{lot}_{side}',
                          'paused': False, 'skip': False, 'state': 'pending',
                          'quantity': 1, 'stop_multiplier': 2, 'trade_path': ''})()
            for lot in TRANCHE_LOTS for side in TRANCHE_SIDES
        ]

    for row in row_iter:
        lot, side = row.lot, row.side
        slot_key = row.slot_key
        trade = _resolve_trade_for_row(row, active_trades, for_date=today) if session_plan else pick_best_trade(
            [t for t in active_trades if _match_trade_to_slot(t, lot, side, for_date=today)]
        )

        slot = {
            'lot': lot,
            'side': side,
            'slot_key': slot_key,
            'session_state': getattr(row, 'state', 'pending'),
            'stop_multiplier': getattr(row, 'stop_multiplier', 2),
            'paused': getattr(row, 'paused', False),
            'error': getattr(row, 'error', '') or '',
        }
        if session_plan:
            slot.update({
                'skip': getattr(row, 'skip', False),
                'plan_quantity': getattr(row, 'quantity', 1),
                'width': getattr(row, 'width', '25-35'),
                'credit_min': getattr(row, 'credit_min', 0.5),
                'credit_max': getattr(row, 'credit_max', 1.0),
                'entry_window_start': getattr(row, 'entry_window_start', ''),
                'entry_window_end': getattr(row, 'entry_window_end', ''),
                'chase1_mode': getattr(row, 'chase1_mode', 'chase_same_trade'),
                'chase1_max': getattr(row, 'chase1_max', 3),
                'chase2_mode': getattr(row, 'chase2_mode', 'build_new_strikes'),
                'chase2_max': getattr(row, 'chase2_max', 7),
            })

        if session_plan:
            slot['state'] = _session_display_state(row, trade)
        elif trade:
            entry = trade.get('entry') or {}
            slot['state'] = _slot_state_from_trade(
                trade.get('status', 'unknown'),
                trade.get('close_mechanism'),
                strategy=entry.get('strategy'),
                trade=trade,
            )
        else:
            slot['state'] = 'pending'

        if trade:
            status, live_pnl, entry, short_sym, long_sym, short_leg, long_leg, quantity, net_credit, trade = (
                _apply_trade_overlay(slot, trade, lot, side, spx_settle=spx_settle)
            )
            if session_plan:
                slot['state'] = _session_display_state(row, trade)

            counts_pnl = (
                _counts_toward_meic_day_pnl(row, trade, slot['state'])
                if session_plan else _trade_has_position(trade)
            )

            if status == 'closed':
                slip_usd = slippage_dollars(trade)
                if slip_usd is not None and counts_pnl:
                    day_slippage += slip_usd

            if counts_pnl:
                if status == 'closed':
                    closed_count += 1
                    day_pnl += live_pnl
                    _sync_key = (lot, side)
                    if _sync_key not in _synced_trades:
                        try:
                            upsert_trade({
                                'date_opened': entry.get('timestamp', '')[:10],
                                'time_opened': entry.get('timestamp', ''),
                                'lot': lot, 'side': side,
                                'short_symbol': short_sym, 'long_symbol': long_sym,
                                'quantity': quantity,
                                'filled_price': net_credit,
                                'short_open_price': short_leg.get('fill_price', 0),
                                'long_open_price': long_leg.get('fill_price', 0),
                                'short_close_price': trade.get('short_close_price'),
                                'long_close_price': trade.get('long_close_price'),
                                'strategy': trades_layout.STRATEGY_MEIC,
                                'paper': 1 if (trade.get('paper') if trade.get('paper') is not None else _strategy_paper_flag(trades_layout.STRATEGY_MEIC)) else 0,
                            })
                            _synced_trades.add(_sync_key)
                        except Exception:
                            pass
                else:
                    open_count += 1
                    day_pnl += live_pnl
            else:
                slot['live_pnl'] = None
                slot['pnl_class'] = ''

        grid.append(slot)

    heartbeat_data = read_json_safe(os.path.join(TRADES_DIR, 'heartbeat.json'))
    streamer_health = read_json_safe(os.path.join(TRADES_DIR, 'streamer_health.json'))
    mqtt_cache_health = read_json_safe(os.path.join(TRADES_DIR, 'mqtt_cache_health.json'))

    manual_rows, manual_pnl, manual_open, manual_slippage = build_manual_trades(
        live_price_fn=_live_price,
        phase_display_fn=_phase_display,
        trade_pnl_fn=_trade_pnl,
        stop_label_fn=_stop_price_label,
        slot_state_fn=_slot_state_from_trade,
        spx_settle=spx_settle,
    )
    from common.trading_gate import summary_for_dashboard

    trading_gate = summary_for_dashboard()
    skyline_pnl = _skyline_today_pnl()
    perma_bull_pnl = _perma_bull_today_pnl(spx_settle=spx_settle)
    iron_fly_pnl = _iron_fly_today_pnl()
    iron_fly_xsp_pnl = _iron_fly_xsp_today_pnl()
    drma_hm_pnl = _drma_hm_today_pnl()
    drma_stocks_pnl = _drma_stocks_today_pnl()
    if session_plan:
        meic_unpaused = sum(1 for r in session_plan.rows if not r.paused and not r.skip)
        all_slot_keys = {r.slot_key for r in session_plan.rows}
    else:
        all_slot_keys = {f'{lot}_{side}' for lot in TRANCHE_LOTS for side in TRANCHE_SIDES}
        meic_unpaused = len(all_slot_keys)

    return {
        'grid':          grid,
        'manual_trades': manual_rows,
        'manual_pnl':    manual_pnl,
        'manual_slippage': manual_slippage,
        'manual_open':   manual_open,
        'meic_pnl':      round(day_pnl, 2),
        'meic_paper':    _strategy_paper_flag(trades_layout.STRATEGY_MEIC),
        'skyline_pnl':   round(skyline_pnl, 2),
        'perma_bull_pnl': round(perma_bull_pnl, 2),
        'iron_fly_pnl':  round(iron_fly_pnl, 2),
        'iron_fly_xsp_pnl': round(iron_fly_xsp_pnl, 2),
        'drma_hm_pnl':   round(drma_hm_pnl, 2),
        'drma_hm_paper': _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM),
        'drma_stocks_pnl': round(drma_stocks_pnl, 2),
        'drma_stocks_paper': True,
        'inverse_meic_paper': _strategy_paper_flag(trades_layout.STRATEGY_INVERSE_MEIC),
        'combined_pnl':  round(day_pnl + manual_pnl + perma_bull_pnl, 2),
        'day_slippage':  round(day_slippage, 2),
        'meic_unpaused_slots': meic_unpaused,
        'meic_slots_total': len(all_slot_keys),
        'day_pnl':       round(day_pnl, 2),
        'day_pnl_class': 'text-success' if day_pnl >= 0 else 'text-danger',
        'open_count':    open_count,
        'closed_count':  closed_count,
        'bot_running':   _launcher_active(),
        'bot_status':    read_bot_status(),
        'spx':           live_prices.get(INDEX_TOPIC, '–'),
        'timestamp':     _central_now_str(),
        'system_health': {
            'launcher': read_bot_status(),
            'streamer_log_age': _log_age_seconds(_stream_log_path()),
            'streamer_health': streamer_health or {},
            'stop_monitor': heartbeat_data or {},
            'stop_monitor_mqtt': mqtt_cache_health or {},
            'mqtt_spx_price': live_prices.get(INDEX_TOPIC),
        },
        'trading_gate': trading_gate,
        'meic_settings': get_meic_settings(root=ROOT).to_dict(),
        'strategy_controls': _get_all_strategy_controls(),
    }


def _log_age_seconds(log_path):
    """How many seconds since log file was last modified."""
    if not log_path:
        return -1
    try:
        return round(time.time() - os.path.getmtime(log_path))
    except Exception:
        return -1


# ── MQTT subscription ─────────────────────────────────────────────────────────

def _mqtt_loop():
    def on_connect(client, userdata, flags, rc, properties=None):
        client.subscribe('#')

    def on_message(client, userdata, msg):
        try:
            val = float(msg.payload.decode())
            live_prices[msg.topic] = val
        except Exception:
            pass

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_BROKER_ADDR, 1883, 60)
            client.loop_forever()
        except Exception:
            time.sleep(5)

threading.Thread(target=_mqtt_loop, daemon=True).start()

# ── WebSocket push loop ────────────────────────────────────────────────────────

_push_task_started = False


def emit_summary_update() -> None:
    """Push latest grid to all connected dashboard clients."""
    socketio.emit('update', build_summary())


def _summary_push_loop() -> None:
    """Background task — must run via socketio.start_background_task (not raw thread)."""
    while True:
        try:
            emit_summary_update()
        except Exception:
            log.exception('Dashboard summary push failed')
        socketio.sleep(2)


@socketio.on('connect')
def _on_socket_connect():
    global _push_task_started
    try:
        emit_summary_update()
    except Exception:
        log.exception('Dashboard connect push failed')
    if not _push_task_started:
        _push_task_started = True
        socketio.start_background_task(_summary_push_loop)

# ── Flask routes ──────────────────────────────────────────────────────────────

@app.before_request
def _enforce_dashboard_auth():
    token = os.environ.get('DASHBOARD_AUTH_TOKEN', '').strip()
    if not token:
        return None
    remote = (request.remote_addr or '').strip()
    if remote in ('127.0.0.1', '::1', 'localhost'):
        return None
    provided = (
        request.headers.get('X-Auth-Token', '').strip()
        or request.args.get('token', '').strip()
        or (request.authorization.password if request.authorization else '')
    )
    if provided == token:
        return None
    return Response(
        'Unauthorized — set X-Auth-Token or Basic Auth password',
        401,
        {'WWW-Authenticate': 'Basic realm="MEIC Dashboard"'},
    )


@app.route('/')
def index():
    return render_template('index.html', data=build_summary())

@app.route('/api/summary')
def api_summary():
    return jsonify(build_summary())

def _enrich_sub_strategy(t):
    strat = t.get('strategy_key', '')
    side = str(t.get('side', '')).upper()
    struct = str(t.get('structure', ''))
    lot = str(t.get('lot', '')).lower()
    sub = t.get('sub_strategy')
    sub_label = t.get('sub_strategy_label')

    if not sub:
        if strat in ('MANUAL_SPREAD', 'MEIC_IC'):
            if 'debit' in struct.lower():
                sub = 'CDS' if side == 'C' else 'PDS'
                sub_label = 'Call Debit Spread' if side == 'C' else 'Put Debit Spread'
            else:
                sub = 'CCS' if side == 'C' else 'PCS'
                sub_label = 'Call Credit Spread' if side == 'C' else 'Put Credit Spread'
        elif strat == 'DRMA_HM_SPX':
            if 'dir_' in lot:
                sub = 'Directional Spread'
                sub_label = 'Directional Spread'
            elif 'fut_' in lot:
                sub = 'Futures Spread'
                sub_label = 'Directional Futures'
            else:
                sub = 'Credit Spread'
                sub_label = 'Non-Directional Spread'
        elif strat == 'DRMA_HM_STOCKS':
            if '_cds_' in lot or '_pds_' in lot:
                sub = 'Directional Spread'
                sub_label = 'Directional Spread'
            elif '_pcs_' in lot or '_ccs_' in lot:
                sub = 'Credit Spread'
                sub_label = 'Non-Directional Spread'
            elif '_equity_' in lot or '_stock_' in lot:
                sub = 'Direct Stock'
                sub_label = 'Direct Stock'
            else:
                sub = 'Stock Structure'
                sub_label = 'Stock Structure'
        elif strat == 'PERMA_BULL_PCS':
            sub = 'PCS'
            sub_label = 'Put Credit Spread'
        elif 'IRON_FLY' in strat:
            sub = 'Iron Fly'
            sub_label = 'Iron Fly'
        elif strat == 'SKYLINE_SPX':
            sub = 'Butterfly'
            sub_label = 'Skyline Butterfly'
        elif strat == 'CLONE_TRADES':
            raw_sub = t.get('sub_strategy') or t.get('called_by') or 'TG'
            try:
                from clone_trades.models import classify_sub_strategy
                sub = classify_sub_strategy(str(raw_sub))
                if sub == 'Others':
                    sub = 'TG'
            except Exception:
                sub = 'TG'
            sub_label = sub
        else:
            sub = strat
            sub_label = t.get('strategy_label', strat)

    t['sub_strategy'] = sub
    t['sub_strategy_label'] = sub_label or sub
    return t

def build_all_live_trades():
    from common.session_cleanup import central_today
    from db import get_trades
    open_trades = []
    closed_trades = []
    today = central_today().isoformat()

    # ── OPEN TRADES ──────────────────────────────────────────────────────────
    # 1. MEIC & Manual Spread from summary
    try:
        summary = build_summary()
        # MEIC Open
        meic_paper = _strategy_paper_flag(trades_layout.STRATEGY_MEIC)
        for s in summary.get('grid', []):
            st = str(s.get('state', '')).lower()
            if str(s.get('status', '')).lower() == 'closed':
                continue
            if st in ('open', 'closing', 'breached') and (s.get('trade_path') or s.get('filename') or s.get('short_strike')):
                fn = s.get('filename') or s.get('_filename') or ''
                entry_credit = float(s.get('entry_credit') or 0.0)
                live_spread = float(s.get('live_spread') or 0.0)
                pnl = float(s.get('live_pnl') or 0.0)
                pnl_pct = round(((entry_credit - live_spread) / entry_credit) * 100, 1) if entry_credit > 0 else None
                open_trades.append({
                    'id': f"meic_{s.get('slot_key')}",
                    'strategy_key': 'MEIC_IC',
                    'strategy_label': 'MEIC Iron Condor',
                    'tab_id': 'meic',
                    'paper': meic_paper,
                    'symbol': 'SPX',
                    'lot': s.get('lot'),
                    'side': s.get('side'),
                    'structure': f"{'Call' if s.get('side') == 'C' else 'Put'} Spread ({s.get('lot', '')})",
                    'strikes': f"{s.get('short_strike', '–')}/{s.get('long_strike', '–')}",
                    'quantity': int(s.get('filled') or s.get('quantity') or 1),
                    'entry_price': entry_credit,
                    'current_price': live_spread,
                    'unrealized_pnl': pnl,
                    'unrealized_pnl_pct': pnl_pct,
                    'status': st.upper(),
                    'stop_loss': s.get('stop_multiplier') and f"{s.get('stop_multiplier')}x stop",
                    'profit_target': None,
                    'time_opened': s.get('entry_time'),
                    'filename': fn,
                    'slot_key': s.get('slot_key'),
                    'actions': ['close', 'scale_out']
                })

        # Manual Spread Open
        manual_paper = _strategy_paper_flag(trades_layout.STRATEGY_MANUAL)
        for s in summary.get('manual_trades', []):
            st = str(s.get('state', '')).lower()
            if str(s.get('status', '')).lower() == 'closed':
                continue
            if st in ('open', 'closing', 'breached', 'working', 'pending'):
                fn = s.get('_filename') or s.get('filename') or ''
                is_working = st in ('working', 'pending')
                entry_credit = float(s.get('entry_credit') or 0.0)
                live_spread = float(s.get('live_spread') or 0.0)
                pnl = float(s.get('live_pnl') or 0.0)
                pnl_pct = round(((entry_credit - live_spread) / entry_credit) * 100, 1) if entry_credit > 0 else None
                open_trades.append({
                    'id': f"manual_{fn}",
                    'strategy_key': 'MANUAL_SPREAD',
                    'strategy_label': 'Manual Spread',
                    'tab_id': 'manual',
                    'paper': manual_paper,
                    'symbol': 'SPX',
                    'lot': s.get('lot'),
                    'side': s.get('side'),
                    'structure': f"{'Call' if s.get('side') == 'C' else 'Put'} Spread ({s.get('lot', '')})",
                    'strikes': f"{s.get('short_strike', '–')}/{s.get('long_strike', '–')}",
                    'quantity': int(s.get('filled') or s.get('quantity') or 1),
                    'entry_price': entry_credit,
                    'current_price': live_spread,
                    'unrealized_pnl': pnl,
                    'unrealized_pnl_pct': pnl_pct,
                    'status': st.upper(),
                    'stop_loss': s.get('stop_price') or (s.get('stop_multiplier') and f"{s.get('stop_multiplier')}x stop"),
                    'profit_target': s.get('profit_target'),
                    'time_opened': s.get('entry_time'),
                    'filename': fn,
                    'slot_key': s.get('lot'),
                    'actions': ['cancel'] if is_working else ['close', 'scale_out']
                })
    except Exception as exc:
        log.warning("Live trades: error loading MEIC / Manual open: %s", exc)

    # 2. Clone Trades (Discretionary) Open
    try:
        from clone_trades.engine import get_engine
        eng = get_engine()
        cl_stat = eng.get_status()
        for t in cl_stat.get('active_trades', []):
            st = str(t.get('status', 'OPEN')).upper()
            spec = t.get('spec', {})
            und = spec.get('underlying', 'OPT')
            ot = spec.get('option_type', 'CALL')
            k = spec.get('strike', '')
            exp = spec.get('expiration', '')
            is_p = t.get('paper_mode') if t.get('paper_mode') is not None else ((t.get('sub_strategy') or 'PC') != 'PC')
            is_working = st == 'WORKING'
            entry_px = float(t.get('entry_price') or t.get('webhook_price') or 0.0)
            curr_px = float(t.get('current_price') or entry_px)
            pnl = float(t.get('unrealized_pnl_usd', 0.0))
            pnl_pct = round(((curr_px - entry_px) / entry_px) * 100, 1) if entry_px > 0 else None
            open_trades.append({
                'id': t.get('id'),
                'strategy_key': 'CLONE_TRADES',
                'strategy_label': 'Discretionary',
                'sub_strategy': t.get('sub_strategy') or t.get('called_by') or 'TG',
                'tab_id': 'clone_trades',
                'paper': is_p,
                'symbol': und,
                'side': ot,
                'structure': f"{ot} ${k} ({exp})",
                'strikes': str(k),
                'quantity': int(t.get('remaining_quantity') or t.get('initial_quantity') or 1),
                'entry_price': entry_px,
                'current_price': curr_px,
                'highest_price': t.get('highest_price'),
                'lowest_price': t.get('lowest_price'),
                'unrealized_pnl': pnl,
                'unrealized_pnl_pct': pnl_pct,
                'status': st,
                'stop_loss': t.get('active_stop_price') and f"${t.get('active_stop_price'):.2f} ({t.get('active_stop_pct', -50)}%)",
                'profit_target': t.get('active_pt_price') and f"${t.get('active_pt_price'):.2f}",
                'time_opened': t.get('created_at_cst') or t.get('created_at'),
                'trade_id': t.get('id'),
                'actions': ['cancel'] if is_working else ['close', 'view_ladder', 'edit_ladder']
            })
    except Exception as exc:
        log.warning("Live trades: error loading clone open: %s", exc)

    # 3. Perma Bull PCS Open
    try:
        pb_trade = _perma_bull_trade_for_date(today)
        if pb_trade:
            pb_st = str(pb_trade.get('status', '')).lower()
            pb_cm = str(pb_trade.get('close_mechanism', '')).lower()
            pb_paper = bool(pb_trade.get('paper')) if pb_trade.get('paper') is not None else _strategy_paper_flag(trades_layout.STRATEGY_PERMA_BULL)
            is_closed = (pb_st == 'closed') or (pb_cm in ('profit_target_hit', 'profit_target', 'exchange_stop', 'manual_close')) or (pb_st == 'closing' and pb_trade.get('manual_management'))
            if not is_closed and pb_st in ('open', 'closing', 'working', 'pending'):
                sk = (pb_trade.get('short_leg') or {}).get('strike', '–')
                lk = (pb_trade.get('long_leg') or {}).get('strike', '–')
                astop = pb_trade.get('active_stop') or {}
                credit = float((pb_trade.get('entry') or {}).get('net_credit') or 0.0)
                pnl = _perma_bull_today_pnl()
                open_trades.append({
                    'id': f"perma_{pb_trade.get('lot') or 'today'}",
                    'strategy_key': 'PERMA_BULL_PCS',
                    'strategy_label': 'Perma Bull PCS',
                    'tab_id': 'perma-bull',
                    'paper': pb_paper,
                    'symbol': 'SPX',
                    'lot': pb_trade.get('lot') or f"perma-{today}",
                    'side': 'P',
                    'structure': '5-wide Put Credit Spread',
                    'strikes': f"{sk}/{lk}",
                    'quantity': int(pb_trade.get('quantity') or pb_trade.get('filled_quantity') or 1),
                    'entry_price': credit,
                    'current_price': None,
                    'unrealized_pnl': float(pnl),
                    'unrealized_pnl_pct': None,
                    'status': pb_st.upper(),
                    'stop_loss': astop.get('stop_price') and f"${astop.get('stop_price')}",
                    'profit_target': astop.get('profit_price') and f"${astop.get('profit_price')}",
                    'time_opened': (pb_trade.get('entry') or {}).get('timestamp'),
                    'filename': os.path.basename(pb_trade.get('_filepath', '')),
                    'actions': ['close', 'manage_manually']
                })
    except Exception as exc:
        log.warning("Live trades: error loading Perma Bull open: %s", exc)

    # 4. Skyline SPX Open
    try:
        sky_act = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, ROOT)
        sky_paper = _strategy_paper_flag(trades_layout.STRATEGY_SKYLINE)
        for f in glob.glob(os.path.join(sky_act, "skyline_*.json")):
            with open(f, 'r', encoding='utf-8') as jf:
                d = json.load(jf)
            if str(d.get('status', '')).lower() == 'open':
                open_trades.append({
                    'id': f"skyline_{os.path.basename(f)}",
                    'strategy_key': 'SKYLINE_SPX',
                    'strategy_label': 'Skyline SPX',
                    'tab_id': 'skyline',
                    'paper': sky_paper,
                    'symbol': 'SPX',
                    'side': 'BF',
                    'structure': 'Butterfly Spread(s)',
                    'strikes': '–',
                    'quantity': int(d.get('quantity', 1)),
                    'entry_price': float(d.get('net_debit_total') or 0.0),
                    'current_price': None,
                    'unrealized_pnl': float(d.get('unrealized_pnl_total') or 0.0),
                    'unrealized_pnl_pct': None,
                    'status': 'OPEN',
                    'stop_loss': None,
                    'profit_target': None,
                    'time_opened': d.get('entry_time_et'),
                    'filename': os.path.basename(f),
                    'actions': ['close_all']
                })
    except Exception as exc:
        log.warning("Live trades: error loading Skyline open: %s", exc)

    # 5. DRMA Stocks Open
    try:
        stk_act = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
        for f in glob.glob(os.path.join(stk_act, "*.json")):
            b = os.path.basename(f)
            if b.startswith("session_") or b.startswith("watchlist_"):
                continue
            with open(f, 'r', encoding='utf-8') as jf:
                d = json.load(jf)
            if str(d.get('status', '')).lower() == 'open':
                ticker = d.get('ticker') or 'STOCK'
                is_eq = d.get('trade_type') == 'DIRECT_STOCK' or 'equity' in b
                ep = float(d.get('entry_price') or 0.0)
                cp = float(d.get('current_price') or ep)
                pnl = float(d.get('pnl') or d.get('unrealized_pnl') or 0.0)
                pnl_pct = round(((cp - ep) / ep) * 100, 1) if ep > 0 else None
                open_trades.append({
                    'id': d.get('trade_id') or b,
                    'strategy_key': 'DRMA_HM_STOCKS',
                    'strategy_label': f'DRMA Stocks ({ticker})',
                    'tab_id': 'drma_stocks',
                    'paper': True,
                    'symbol': ticker,
                    'side': d.get('side', 'LONG'),
                    'structure': 'Direct Equity' if is_eq else (d.get('sub_strategy') or 'Spread'),
                    'strikes': f"{d.get('short_strike', '–')}/{d.get('long_strike', '–')}" if not is_eq else f"{d.get('shares', 1)} sh",
                    'quantity': int(d.get('quantity') or d.get('shares') or 1),
                    'entry_price': ep,
                    'current_price': cp,
                    'unrealized_pnl': pnl,
                    'unrealized_pnl_pct': pnl_pct,
                    'status': 'OPEN',
                    'stop_loss': None,
                    'profit_target': None,
                    'time_opened': d.get('entry_time'),
                    'trade_id': d.get('trade_id') or b.replace('.json', ''),
                    'actions': ['close']
                })
    except Exception as exc:
        log.warning("Live trades: error loading DRMA stocks open: %s", exc)

    # 6. DRMA HM SPX Open
    try:
        hm_act = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
        hm_paper = _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM)
        for f in glob.glob(os.path.join(hm_act, "drma_hm_*.json")):
            with open(f, 'r', encoding='utf-8') as jf:
                d = json.load(jf)
            if str(d.get('status', '')).lower() == 'open':
                open_trades.append({
                    'id': d.get('trade_id') or os.path.basename(f),
                    'strategy_key': 'DRMA_HM_SPX',
                    'strategy_label': 'DRMA H-M Pro',
                    'tab_id': 'drma_hm',
                    'paper': hm_paper,
                    'symbol': 'SPX',
                    'side': d.get('side', '–'),
                    'structure': d.get('structure_type') or d.get('sub_strategy') or 'DRMA Structure',
                    'strikes': f"{d.get('short_strike', '–')}/{d.get('long_strike', '–')}",
                    'quantity': int(d.get('quantity', 1)),
                    'entry_price': float(d.get('entry_price') or d.get('credit') or 0.0),
                    'current_price': None,
                    'unrealized_pnl': float(d.get('pnl') or 0.0),
                    'unrealized_pnl_pct': None,
                    'status': 'OPEN',
                    'stop_loss': None,
                    'profit_target': None,
                    'time_opened': d.get('timestamp') or d.get('entry_time'),
                    'trade_id': d.get('trade_id'),
                    'actions': ['close_all']
                })
    except Exception as exc:
        log.warning("Live trades: error loading DRMA HM open: %s", exc)

    # 7. Iron Fly SPX / XSP Open
    for s_name, t_tab in [(trades_layout.STRATEGY_IRON_FLY, 'iron_fly'), (trades_layout.STRATEGY_IRON_FLY_XSP, 'iron_fly_xsp')]:
        try:
            fly_act = trades_layout.active_dir_for_strategy(s_name, ROOT)
            fly_paper = _strategy_paper_flag(s_name)
            sym = 'XSP' if 'XSP' in s_name else 'SPX'
            for f in glob.glob(os.path.join(fly_act, "iron_fly_*.json")):
                with open(f, 'r', encoding='utf-8') as jf:
                    d = json.load(jf)
                if str(d.get('status', '')).lower() == 'open':
                    open_trades.append({
                        'id': f"fly_{os.path.basename(f)}",
                        'strategy_key': s_name,
                        'strategy_label': 'Iron Fly XSP' if 'XSP' in s_name else 'Iron Fly SPX',
                        'tab_id': t_tab,
                        'paper': fly_paper,
                        'symbol': sym,
                        'side': 'IF',
                        'structure': f'Iron Fly {sym}',
                        'strikes': '–',
                        'quantity': int(d.get('quantity', 1)),
                        'entry_price': float(d.get('credit_received') or d.get('open_credit') or 0.0),
                        'current_price': None,
                        'unrealized_pnl': float(d.get('unrealized_pnl') or d.get('pnl') or 0.0),
                        'unrealized_pnl_pct': None,
                        'status': 'OPEN',
                        'stop_loss': None,
                        'profit_target': None,
                        'time_opened': d.get('entry_time') or d.get('placed_time'),
                        'actions': ['close_all']
                    })
        except Exception as exc:
            log.warning("Live trades: error loading Iron Fly open: %s", exc)

    # ── CLOSED TRADES (TODAY) ────────────────────────────────────────────────
    STRATEGY_META = {
        'MEIC_IC': ('MEIC Iron Condor', 'meic'),
        'MANUAL_SPREAD': ('Manual Spread', 'manual'),
        'CLONE_TRADES': ('Discretionary', 'clone_trades'),
        'PERMA_BULL_PCS': ('Perma Bull PCS', 'perma-bull'),
        'SKYLINE_SPX': ('Skyline SPX', 'skyline'),
        'DRMA_HM_STOCKS': ('DRMA Stocks', 'drma_stocks'),
        'DRMA_HM_SPX': ('DRMA H-M Pro', 'drma_hm'),
        'IRON_FLY_SPX': ('Iron Fly SPX', 'iron_fly'),
        'IRON_FLY_XSP': ('Iron Fly XSP', 'iron_fly_xsp'),
        'MEAN_REVERSION_SPX': ('Mean Reversion SPX', 'mean_reversion'),
        'INVERSE_MEIC': ('Inverse MEIC', 'inverse_meic'),
    }

    seen_closed_keys = set()
    try:
        from dashboard.db import get_conn
        with get_conn() as conn:
            sql = """
                SELECT * FROM trades
                WHERE status = 'CLOSED'
                  AND (time_closed LIKE ? OR date_opened = ?)
                ORDER BY COALESCE(time_closed, date_opened) DESC
            """
            db_trades = [dict(r) for r in conn.execute(sql, (f"{today}%", today)).fetchall()]
        for t in db_trades:
            strat = t.get('strategy', '')
            lot = t.get('lot', '')
            dedup_key = f"{strat}_{lot}"
            seen_closed_keys.add(dedup_key)

            label, tab_id = STRATEGY_META.get(strat, (strat, strat.lower()))
            side = t.get('side', '')
            sym = 'SPX'
            if strat == 'CLONE_TRADES':
                sym = t.get('short_symbol', '').split()[0] if t.get('short_symbol') else 'OPT'
            elif 'STOCKS' in strat:
                sym = t.get('short_symbol') or lot or 'STOCK'

            struct_desc = f"{'Call' if side == 'C' else ('Put' if side == 'P' else strat)} Spread ({lot})"
            if strat == 'CLONE_TRADES':
                struct_desc = t.get('short_symbol') or f"Clone Trade ({lot})"

            entry_px = float(t.get('open_credit') or t.get('short_open_price') or 0.0)
            exit_px = float(t.get('close_debit') or t.get('short_close_price') or 0.0)
            pnl = float(t.get('pnl') or 0.0)
            is_meic_paper = (strat == 'MEIC_IC' and _strategy_paper_flag(trades_layout.STRATEGY_MEIC))
            is_trade_paper = bool(t.get('paper')) or is_meic_paper

            closed_trades.append({
                'id': f"closed_db_{t.get('id')}",
                'strategy_key': strat,
                'strategy_label': label,
                'tab_id': tab_id,
                'paper': is_trade_paper,
                'symbol': sym,
                'lot': lot,
                'side': side,
                'structure': struct_desc,
                'strikes': f"{t.get('short_symbol', '')} {t.get('long_symbol', '')}".strip() or '–',
                'quantity': int(t.get('quantity') or 1),
                'entry_price': entry_px,
                'exit_price': exit_px,
                'realized_pnl': pnl,
                'status': 'CLOSED',
                'time_opened': t.get('time_opened'),
                'time_closed': t.get('time_closed') or t.get('created_at'),
            })
    except Exception as exc:
        log.warning("Live trades: error loading DB closed trades: %s", exc)

    # 2. Closed Manual Spreads from summary (all 10 closed, PT, breached)
    try:
        manual_paper = _strategy_paper_flag(trades_layout.STRATEGY_MANUAL)
        for s in summary.get('manual_trades', []):
            st = str(s.get('status', '')).lower()
            state = str(s.get('state', '')).lower()
            if st == 'closed' or state in ('closed', 'profit_target', 'breached'):
                lot = s.get('lot', '')
                side = s.get('side', '')
                dedup_key = f"MANUAL_SPREAD_{lot}"
                if dedup_key in seen_closed_keys:
                    continue
                seen_closed_keys.add(dedup_key)

                entry_credit = float(s.get('entry_credit') or s.get('limit_credit') or 0.0)
                exit_px = 0.0
                exit_label = s.get('exit_label', '')
                if exit_label:
                    try:
                        exit_px = float(exit_label.split()[0].replace('$', ''))
                    except Exception:
                        pass
                pnl = float(s.get('live_pnl') or 0.0)
                strikes = f"{s.get('short_strike', '–')}/{s.get('long_strike', '–')}"

                closed_trades.append({
                    'id': f"closed_manual_{lot}_{side}",
                    'strategy_key': 'MANUAL_SPREAD',
                    'strategy_label': 'Manual Spread',
                    'tab_id': 'manual',
                    'paper': manual_paper,
                    'symbol': 'SPX',
                    'lot': lot,
                    'side': side,
                    'structure': f"{'Call' if side == 'C' else 'Put'} Spread ({lot})",
                    'strikes': strikes,
                    'quantity': int(s.get('filled') or s.get('quantity') or 1),
                    'entry_price': entry_credit,
                    'exit_price': exit_px,
                    'realized_pnl': pnl,
                    'status': 'CLOSED',
                    'time_opened': s.get('entry_time'),
                    'time_closed': s.get('exit_time'),
                })
    except Exception as exc:
        log.warning("Live trades: error loading manual closed from summary: %s", exc)

    # 3. Closed Perma Bull PCS
    try:
        pb_trade = _perma_bull_trade_for_date(today)
        if pb_trade:
            pb_st = str(pb_trade.get('status', '')).lower()
            pb_cm = str(pb_trade.get('close_mechanism', '')).lower()
            pb_paper = bool(pb_trade.get('paper')) if pb_trade.get('paper') is not None else _strategy_paper_flag(trades_layout.STRATEGY_PERMA_BULL)
            lot = str(pb_trade.get('lot') or f"perma-{today}")
            side = 'P'
            dedup_key = f"PERMA_BULL_PCS_{lot}"
            is_closed = (pb_st == 'closed') or (pb_cm in ('profit_target_hit', 'profit_target', 'exchange_stop', 'manual_close')) or (pb_st == 'closing' and pb_trade.get('manual_management'))
            if is_closed and dedup_key not in seen_closed_keys:
                seen_closed_keys.add(dedup_key)
                sk = (pb_trade.get('short_leg') or {}).get('strike', '–')
                lk = (pb_trade.get('long_leg') or {}).get('strike', '–')
                credit = float((pb_trade.get('entry') or {}).get('net_credit') or 0.0)
                pnl = _perma_bull_today_pnl()
                exit_px = 0.0
                close_info = pb_trade.get('close') or {}
                if close_info.get('close_price') is not None:
                    exit_px = float(close_info['close_price'])
                elif (pb_trade.get('exit_config') or {}).get('profit_target_debit') is not None:
                    exit_px = float(pb_trade['exit_config']['profit_target_debit'])

                closed_trades.append({
                    'id': f"closed_perma_{lot}",
                    'strategy_key': 'PERMA_BULL_PCS',
                    'strategy_label': 'Perma Bull PCS',
                    'tab_id': 'perma-bull',
                    'paper': pb_paper,
                    'symbol': 'SPX',
                    'lot': lot,
                    'side': side,
                    'structure': '5-wide Put Credit Spread',
                    'strikes': f"{sk}/{lk}",
                    'quantity': int(pb_trade.get('quantity') or pb_trade.get('filled_quantity') or 1),
                    'entry_price': credit,
                    'exit_price': exit_px,
                    'realized_pnl': float(pnl),
                    'status': 'CLOSED',
                    'time_opened': (pb_trade.get('entry') or {}).get('timestamp'),
                    'time_closed': (pb_trade.get('close') or {}).get('timestamp') or pb_trade.get('recovery', {}).get('last_heartbeat'),
                })
    except Exception as exc:
        log.warning("Live trades: error loading Perma Bull closed: %s", exc)

    # 4. Closed DRMA HM SPX Live Trades from disk
    try:
        hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
        for path in glob.glob(os.path.join(hist_dir, f"drma_hm_{today}_*.json")):
            with open(path, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if d.get('paper') is False and str(d.get('status', '')).lower() == 'closed':
                trade_id = d.get('trade_id') or os.path.basename(path).replace('.json', '')
                side = d.get('side', 'P')
                dedup_key = f"DRMA_HM_SPX_{trade_id}"
                if dedup_key in seen_closed_keys:
                    continue
                seen_closed_keys.add(dedup_key)
                closed_trades.append({
                    'id': f"closed_drma_{trade_id}",
                    'strategy_key': 'DRMA_HM_SPX',
                    'strategy_label': 'DRMA H-M Pro',
                    'tab_id': 'drma_hm',
                    'paper': False,
                    'symbol': 'SPX',
                    'lot': trade_id,
                    'side': side,
                    'structure': d.get('structure_type') or d.get('sub_strategy') or 'Credit Spread',
                    'strikes': f"{d.get('short_strike', '–')}/{d.get('long_strike', '–')}",
                    'quantity': int(d.get('quantity') or 1),
                    'entry_price': float(d.get('entry_price') or d.get('net_credit') or 0.0),
                    'exit_price': float(d.get('exit_price') or d.get('close_price') or 0.0),
                    'realized_pnl': float(d.get('realized_pnl_dollars') or d.get('realized_pnl') or d.get('pnl') or 0.0),
                    'status': 'CLOSED',
                    'time_opened': d.get('entered_at'),
                    'time_closed': d.get('exited_at') or d.get('closed_at'),
                })
    except Exception as exc:
        log.warning("Live trades: error loading DRMA HM history disk: %s", exc)

    try:
        from clone_trades.engine import get_engine
        cl_stat = get_engine().get_status()
        for t in cl_stat.get('today_closed_trades', []):
            trade_id = t.get('id', '')
            dedup_key = f"CLONE_TRADES_{trade_id}"
            if dedup_key in seen_closed_keys:
                continue
            seen_closed_keys.add(dedup_key)
            spec = t.get('spec', {})
            und = spec.get('underlying', 'OPT')
            ot = spec.get('option_type', 'CALL')
            k = spec.get('strike', '')
            exp = spec.get('expiration', '')
            is_p = t.get('paper_mode') if t.get('paper_mode') is not None else True
            entry_px = float(t.get('entry_price') or t.get('webhook_price') or 0.0)
            closed_trades.append({
                'id': f"closed_clone_{trade_id}",
                'strategy_key': 'CLONE_TRADES',
                'strategy_label': 'Discretionary',
                'sub_strategy': t.get('sub_strategy') or t.get('called_by') or 'TG',
                'tab_id': 'clone_trades',
                'paper': is_p,
                'symbol': und,
                'lot': trade_id,
                'side': ot,
                'structure': f"{ot} ${k} ({exp})",
                'strikes': str(k),
                'quantity': int(t.get('initial_quantity') or 1),
                'entry_price': entry_px,
                'exit_price': float(t.get('exit_price') or 0.0),
                'realized_pnl': float(t.get('realized_pnl_usd', 0.0)),
                'status': 'CLOSED',
                'time_opened': t.get('created_at_cst') or t.get('created_at'),
                'time_closed': t.get('closed_at_cst') or t.get('closed_at'),
            })
    except Exception as exc:
        log.warning("Live trades: error loading clone closed: %s", exc)

    # User instruction: Filter to ONLY trades that are live, not paper
    open_trades = [_enrich_sub_strategy(t) for t in open_trades if not t.get('paper')]
    closed_trades = [_enrich_sub_strategy(t) for t in closed_trades if not t.get('paper')]

    total_unrealized = sum(t.get('unrealized_pnl') or 0.0 for t in open_trades)
    total_realized = sum(t.get('realized_pnl') or 0.0 for t in closed_trades)
    breached_count = sum(1 for t in open_trades if t.get('status') == 'BREACHED')
    working_count = sum(1 for t in open_trades if t.get('status') in ('WORKING', 'PENDING'))

    open_strategy_pnl = {}
    open_strategy_counts = {}
    for t in open_trades:
        sk = t.get('strategy_key', 'UNKNOWN')
        open_strategy_counts[sk] = open_strategy_counts.get(sk, 0) + 1
        open_strategy_pnl[sk] = round(open_strategy_pnl.get(sk, 0.0) + (t.get('unrealized_pnl') or 0.0), 2)

    closed_strategy_pnl = {}
    closed_strategy_counts = {}
    for t in closed_trades:
        sk = t.get('strategy_key', 'UNKNOWN')
        closed_strategy_counts[sk] = closed_strategy_counts.get(sk, 0) + 1
        closed_strategy_pnl[sk] = round(closed_strategy_pnl.get(sk, 0.0) + (t.get('realized_pnl') or 0.0), 2)

    return {
        'trades': open_trades,
        'open_trades': open_trades,
        'closed_trades': closed_trades,
        'stats': {
            'total_trades': len(open_trades),
            'open_count': len(open_trades),
            'closed_count': len(closed_trades),
            'live_count': len(open_trades),
            'paper_count': 0,
            'breached_count': breached_count,
            'working_count': working_count,
            'total_unrealized_pnl': round(total_unrealized, 2),
            'total_realized_pnl': round(total_realized, 2),
            'strategy_counts': open_strategy_counts,
            'open_strategy_pnl': open_strategy_pnl,
            'closed_strategy_pnl': closed_strategy_pnl,
            'open_strategy_counts': open_strategy_counts,
            'closed_strategy_counts': closed_strategy_counts,
        },
        'updated_at': _central_now_str()
    }

@app.route('/api/live_trades')
def api_live_trades():
    return jsonify(build_all_live_trades())

@app.route('/api/log/<name>')
def api_log(name):
    from dashboard.process_manager import get_service_status, SERVICES
    safe_name = os.path.basename(name or '').replace('..', '')
    if not safe_name:
        return jsonify({'error': 'invalid log name'}), 400
    paths = {
        'launcher': _launcher_log_path(),
        'stream': _stream_log_path(),
        'streamer': _stream_log_path(),
        'stop_monitor': STOP_MONITOR_LOG,
    }
    path = paths.get(safe_name)
    if not path and safe_name in SERVICES:
        svc_st = get_service_status(safe_name)
        path = svc_st.get('log_path')
    if not path:
        pattern = os.path.join(ROOT, 'logs', f'*{safe_name}*.log')
        matches = sorted(glob.glob(pattern))
        if matches:
            path = matches[-1]
    if not path:
        return jsonify({'error': 'unknown log'}), 404
    if not os.path.isfile(path):
        return jsonify({'log': '(log not available)'})
    return jsonify({'log': tail_log(path, n=60), 'path': path})

@app.route('/api/lot_logs')
def api_lot_logs():
    log_dir = os.path.join(ROOT, 'meic0dte', 'logs')
    result = {}
    try:
        for fname in sorted(os.listdir(log_dir)):
            if fname.endswith('.log'):
                result[fname] = tail_log(os.path.join(log_dir, fname), 20)
    except Exception:
        pass
    return jsonify(result)

@app.route('/api/start_bot', methods=['POST'])
def start_bot():
    global bot_process
    from common.process_lock import active_lock_pid

    with _state_lock:
        lock_pid = active_lock_pid('launcher')
        if lock_pid is not None:
            return jsonify({
                'status': 'already_running_external_lock',
                'pid': lock_pid,
            }), 409
        if _launcher_active():
            status = read_bot_status()
            return jsonify({
                'status': 'already_running_external',
                'reason': 'Launcher already active',
                'bot_status': status,
            }), 409
        if bot_process is not None and bot_process.poll() is None:
            return jsonify({'status': 'already_running'})
        bot_process = subprocess.Popen(
            [sys.executable, os.path.join(ROOT, 'run.py')],
            cwd=ROOT
        )
    return jsonify({'status': 'started', 'pid': bot_process.pid})


@app.route('/api/broker_health')
def broker_health():
    from common import broker_cooldown
    from common.process_lock import list_locks
    from common.rest_limiter import get_rest_limiter
    from common.broker_factory import shared_broker_stats
    from common.trading_gate import summary_for_dashboard
    from dashboard.broker_fill_sync import fill_sync_stats

    from common.rest_metrics import metrics_snapshot

    locks = [
        {
            'name': lk.name,
            'pid': lk.pid,
            'alive': lk.alive,
            'meta': lk.meta,
        }
        for lk in list_locks()
    ]
    return jsonify({
        'fill_sync': fill_sync_stats(),
        'cooldown': broker_cooldown.cooldown_snapshot(),
        'rest': get_rest_limiter().stats(),
        'rest_metrics': metrics_snapshot(),
        'shared_broker': shared_broker_stats(),
        'locks': locks,
        'launcher_active': _launcher_active(),
        'dashboard_pid': os.getpid(),
        'trading_gate': summary_for_dashboard(),
    })


@app.route('/api/rest-probe', methods=['POST'])
def api_rest_probe():
    from common.broker_factory import get_broker
    from common.rest_probe import run_rest_probe
    from common.trading_gate import summary_for_dashboard

    try:
        result = run_rest_probe(get_broker(), source='dashboard', bypass_local_cooldown=True)
    except Exception as exc:
        return jsonify({'status': 'error', 'error': str(exc)}), 500
    return jsonify({
        'status': 'ok' if result.ok else 'failed',
        'probe': {
            'ok': result.ok,
            'status': result.status,
            'latency_ms': result.latency_ms,
            'http_status': result.http_status,
            'detail': result.detail,
        },
        'trading_gate': summary_for_dashboard(),
    })


@app.route('/api/trading-gate/resume', methods=['POST'])
def api_trading_gate_resume():
    from common.trading_gate import resume_new_risk, summary_for_dashboard

    decision = resume_new_risk(cleared_by='dashboard')
    if decision.blocked:
        return jsonify({
            'status': 'rejected',
            'reason': decision.reason,
            'detail': decision.detail,
            'trading_gate': summary_for_dashboard(),
        }), 423
    return jsonify({
        'status': 'ok',
        'trading_gate': summary_for_dashboard(),
    })

@app.route('/api/stop_bot', methods=['POST'])
def stop_bot():
    global bot_process
    with _state_lock:
        if bot_process is not None and bot_process.poll() is None:
            bot_process.terminate()
            bot_process.wait(timeout=5)
            bot_process = None
        try:
            with open(BOT_STATUS_FILE, 'w') as f:
                json.dump({'state': 'kill', 'reason': 'Stopped via dashboard', 'ts': _central_now_str()}, f)
        except Exception:
            pass
    return jsonify({'status': 'stopped'})

@app.route('/api/killswitch', methods=['POST'])
def killswitch():
    """Write killswitch sentinel — stop_monitor forces breach on all active trades."""
    sentinel_path = os.path.join(TRADES_DIR, 'killswitch.json')
    try:
        os.makedirs(TRADES_DIR, exist_ok=True)
        with open(sentinel_path, 'w') as f:
            json.dump({
                'action': 'kill_all',
                'close_mechanism': 'admin_killswitch',
                'ts': _central_now_str(),
                'source': 'dashboard',
            }, f)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500
    return jsonify({'status': 'killswitch_written'})

@app.route('/api/close_trade', methods=['POST'])
def close_trade():
    """Write command file to force-breach a single trade."""
    data = request.get_json(force=True)
    filename = data.get('filename', '')
    if not filename:
        return jsonify({'error': 'filename required'}), 400
    cmd_dir = commands_dir_for_filename(filename, TRADES_DIR)
    cmd_path = os.path.join(cmd_dir, f'{filename}.close.json')
    try:
        with open(cmd_path, 'w') as f:
            json.dump({
                'action': 'close',
                'close_mechanism': 'manual_close',
                'ts': _central_now_str(),
                'source': 'dashboard',
            }, f)
    except Exception as e:
        return jsonify({'status': 'error', 'error': str(e)}), 500
    return jsonify({'status': 'close_command_written', 'filename': filename})

@app.route('/api/scale_out', methods=['POST'])
def scale_out():
    """Scale out a partial quantity of an active spread trade."""
    data = request.get_json(force=True) or {}
    filename = (data.get('filename') or '').strip()
    qty = data.get('quantity')
    debit_limit = data.get('profit_target') or data.get('debit_limit')
    stop_loss = data.get('stop_loss') or data.get('stop_debit')
    if not filename:
        return jsonify({'status': 'error', 'error': 'filename required'}), 400
    if qty is None:
        return jsonify({'status': 'error', 'error': 'quantity required'}), 400

    try:
        from common.scale_out import scale_out_spread
        from common.broker_factory import get_shared_broker
        broker = get_shared_broker()
        result = scale_out_spread(
            broker,
            live_prices,
            filename,
            close_qty=int(qty),
            debit_limit=float(debit_limit) if debit_limit is not None and float(debit_limit) > 0 else None,
            stop_loss=float(stop_loss) if stop_loss is not None and float(stop_loss) > 0 else None,
            root=ROOT,
        )
        code = 200 if result.get('status') == 'ok' else 400
        if code == 200:
            emit_summary_update()
        return jsonify(result), code
    except Exception as exc:
        log.exception('Scale out failed for %s', filename)
        return jsonify({'status': 'error', 'error': str(exc)}), 500

@app.route('/api/pause_tranches', methods=['POST'])
def pause_tranches():
    """Pause or unpause specific tranche slots via session CSV."""
    data = request.get_json(force=True)
    slots = data.get('slots', [])
    action = data.get('action', 'pause')

    plan = _patch_session_rows(slots, action)
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404

    csv_paused = [r.slot_key for r in plan.rows if r.paused]
    return jsonify({'status': 'ok', 'paused_slots': csv_paused})


@app.route('/api/session/meic')
def api_session_meic():
    plan = _meic_session_rows()
    if plan is None:
        return jsonify({'rows': [], 'path': ''})
    return jsonify({'rows': [r.to_dict() for r in plan.rows], 'path': plan.path})


_SESSION_ROW_ALLOWED = frozenset({
    'paused', 'skip', 'quantity', 'stop_multiplier', 'stop_percent',
    'width', 'credit_min', 'credit_max', 'chase1_mode', 'chase1_max',
    'chase2_mode', 'chase2_max', 'fill_wait_sec', 'max_attempts',
    'entry_window_start', 'entry_window_end',
})
_SESSION_WINDOW_KEYS = frozenset({'entry_window_start', 'entry_window_end'})
_SESSION_FIELD_EDITABLE_STATES = frozenset({'pending', 'entering'})
_SESSION_WINDOW_EDITABLE_STATES = frozenset({'pending', 'entering', 'failed', 'skipped'})


def _normalize_session_window_fields(updates: dict) -> tuple[dict | None, str | None]:
    from blocks.session.plan import format_time_field, parse_time_field

    out = dict(updates)
    for time_key in _SESSION_WINDOW_KEYS:
        if time_key not in out:
            continue
        try:
            out[time_key] = format_time_field(parse_time_field(str(out[time_key])))
        except ValueError as exc:
            return None, str(exc)
    return out, None


def _session_row_patch_allowed(state: str, updates: dict) -> bool:
    """Paused-only edits are always allowed; window fields also on failed/skipped."""
    non_pause = {k for k in updates if k != 'paused'}
    if not non_pause:
        return True
    if non_pause.issubset(_SESSION_WINDOW_KEYS) and state in _SESSION_WINDOW_EDITABLE_STATES:
        return True
    return state in _SESSION_FIELD_EDITABLE_STATES


def _session_row_patch_extras(state: str, updates: dict) -> dict:
    """Re-arm failed/skipped rows when operator shifts today's entry window."""
    extras: dict = {}
    if (
        state in ('failed', 'skipped')
        and _SESSION_WINDOW_KEYS.intersection(updates)
    ):
        extras['state'] = 'pending'
    return extras


@app.route('/api/session/row', methods=['PATCH'])
def api_session_row_patch():
    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    slot_key = data.get('slot_key')
    fields = data.get('fields') or {}
    if not slot_key:
        return jsonify({'error': 'slot_key required'}), 400
    if strategy != trades_layout.STRATEGY_MEIC:
        return jsonify({'error': 'only MEIC_IC supported in 4c'}), 400

    plan = _meic_session_rows()
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404
    row = plan.row_by_slot_key(slot_key)
    if row is None:
        return jsonify({'error': f'unknown slot_key: {slot_key}'}), 404

    updates = {k: v for k, v in fields.items() if k in _SESSION_ROW_ALLOWED}
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400

    updates, err = _normalize_session_window_fields(updates)
    if err:
        return jsonify({'error': err}), 400

    if not _session_row_patch_allowed(row.state, updates):
        return jsonify({'error': f'row state {row.state} is not editable'}), 400

    updates.update(_session_row_patch_extras(row.state, updates))
    plan.update_row(slot_key, **updates)
    plan.save()
    if strategy == trades_layout.STRATEGY_MEIC:
        from blocks.session.session_defaults import save_session_defaults
        save_session_defaults(slot_key, updates, root=ROOT)
    return jsonify({'status': 'ok', 'row': plan.row_by_slot_key(slot_key).to_dict()})


@app.route('/api/session/lot-window', methods=['PATCH'])
def api_session_lot_window_patch():
    """Apply entry window to all editable rows in a lot (today's session CSV only)."""
    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    lot = (data.get('lot') or '').strip()
    fields = data.get('fields') or {}
    if not lot:
        return jsonify({'error': 'lot required'}), 400
    if strategy != trades_layout.STRATEGY_MEIC:
        return jsonify({'error': 'only MEIC_IC supported in 4c'}), 400

    updates = {k: v for k, v in fields.items() if k in _SESSION_WINDOW_KEYS}
    if len(updates) != 2:
        return jsonify({'error': 'entry_window_start and entry_window_end required'}), 400

    plan = _meic_session_rows()
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404

    updates, err = _normalize_session_window_fields(updates)
    if err:
        return jsonify({'error': err}), 400

    updated: list[str] = []
    skipped: list[str] = []
    for row in plan.rows:
        if row.lot != lot:
            continue
        if not _session_row_patch_allowed(row.state, updates):
            skipped.append(row.slot_key)
            continue
        patch = dict(updates)
        patch.update(_session_row_patch_extras(row.state, patch))
        plan.update_row(row.slot_key, **patch)
        updated.append(row.slot_key)

    if not updated:
        return jsonify({'error': f'no editable rows for lot {lot}'}), 400

    plan.save()
    if strategy == trades_layout.STRATEGY_MEIC:
        from blocks.session.session_defaults import save_bulk_session_defaults
        save_bulk_session_defaults({sk: updates for sk in updated}, root=ROOT)
    return jsonify({'status': 'ok', 'lot': lot, 'updated': updated, 'skipped': skipped})


_MASTER_PLAN_FIELDS = frozenset({
    'quantity', 'width', 'credit_min', 'credit_max', 'stop_multiplier',
    'chase1_mode', 'chase1_max', 'chase2_mode', 'chase2_max',
})


@app.route('/api/session/bulk', methods=['PATCH'])
def api_session_bulk_patch():
    """Apply shared session-plan fields to all pending/entering MEIC rows."""
    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    fields = data.get('fields') or {}
    if strategy != trades_layout.STRATEGY_MEIC:
        return jsonify({'error': 'only MEIC_IC supported in 4c'}), 400

    updates = {k: v for k, v in fields.items() if k in _MASTER_PLAN_FIELDS}
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400

    plan = _meic_session_rows()
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404

    updated: list[str] = []
    skipped: list[str] = []
    for row in plan.rows:
        if row.state not in ('pending', 'entering'):
            skipped.append(row.slot_key)
            continue
        plan.update_row(row.slot_key, **updates)
        updated.append(row.slot_key)

    if not updated:
        return jsonify({'error': 'no editable rows (all entered or closed)'}), 400

    plan.save()
    if strategy == trades_layout.STRATEGY_MEIC:
        from blocks.session.session_defaults import save_bulk_session_defaults
        save_bulk_session_defaults({sk: updates for sk in updated}, root=ROOT)
    return jsonify({'status': 'ok', 'updated': updated, 'skipped': skipped})


@app.route('/api/meic/settings', methods=['GET', 'POST', 'PATCH'])
def api_meic_settings():
    from common.meic_settings import (
        get_meic_settings,
        save_meic_settings,
        get_meic_tranche_credits,
        reconcile_meic_active_stops,
    )
    current = get_meic_settings(root=ROOT)
    if request.method == 'GET':
        return jsonify({'status': 'ok', 'settings': current.to_dict()})

    data = request.get_json(force=True) or {}
    prev_oco = current.use_oco_15c
    prev_aggressive = current.aggressive_side
    prev_ccs = current.side_ccs_enabled
    prev_pcs = current.side_pcs_enabled

    if 'use_oco_15c' in data:
        current.use_oco_15c = bool(data['use_oco_15c'])
    if 'aggressive_side' in data:
        side_val = str(data['aggressive_side']).strip().upper()
        if side_val in ('P', 'C', 'NONE'):
            current.aggressive_side = side_val
    if 'aggressive_pct' in data:
        try:
            current.aggressive_pct = float(data['aggressive_pct'])
        except (ValueError, TypeError):
            pass
    if 'side_ccs_enabled' in data:
        current.side_ccs_enabled = bool(data['side_ccs_enabled'])
    if 'side_pcs_enabled' in data:
        current.side_pcs_enabled = bool(data['side_pcs_enabled'])
    if 'paper_mode' in data:
        current.paper_mode = bool(data['paper_mode'])
        os.environ['PAPER_MODE'] = 'true' if current.paper_mode else 'false'
        tt_config.PAPER_MODE = current.paper_mode
    if 'companion_breach_tightening' in data:
        current.companion_breach_tightening = bool(data['companion_breach_tightening'])

    save_meic_settings(current, root=ROOT)

    reconciled_trades = []
    if current.use_oco_15c != prev_oco:
        try:
            from common.broker_factory import get_shared_broker_for_strategy
            paper = _strategy_paper_flag(trades_layout.STRATEGY_MEIC)
            broker = get_shared_broker_for_strategy(trades_layout.STRATEGY_MEIC, paper=paper)
            if broker:
                reconciled_trades = reconcile_meic_active_stops(broker, current.use_oco_15c, root=ROOT)
        except Exception as exc:
            log.warning('Could not reconcile stops on OCO toggle: %s', exc)

    # If side enabled states or aggressive side changed, update pending rows in session plan
    plan = _meic_session_rows()
    plan_updated = False
    if plan:
        for row in plan.rows:
            if row.state == 'pending':
                # Update paused if side toggles changed
                if row.side == 'C' and current.side_ccs_enabled != prev_ccs:
                    row.paused = not current.side_ccs_enabled
                    plan_updated = True
                elif row.side == 'P' and current.side_pcs_enabled != prev_pcs:
                    row.paused = not current.side_pcs_enabled
                    plan_updated = True

                # Update credit bounds if aggressive side changed
                if current.aggressive_side != prev_aggressive or 'aggressive_pct' in data:
                    c_min, c_max = get_meic_tranche_credits(row.lot, row.side, settings=current, root=ROOT)
                    row.credit_min = c_min
                    row.credit_max = c_max
                    plan_updated = True
        if plan_updated:
            plan.save()

    emit_summary_update()
    return jsonify({
        'status': 'ok',
        'settings': current.to_dict(),
        'reconciled_trades': reconciled_trades,
    })


@app.route('/api/session/manual')
def api_session_manual():
    from blocks.session.plan import ensure_manual_session

    ensure_manual_session(ROOT)
    plan = load_manual_session_today(ROOT)
    if plan is None:
        return jsonify({'rows': [], 'path': ''})
    return jsonify({'rows': [r.to_dict() for r in plan.rows], 'path': plan.path})


@app.route('/api/session/manual/place', methods=['POST'])
def api_session_manual_place():
    from blocks.session.manual_helpers import dispatch_manual_place

    data = request.get_json(force=True) or {}
    try:
        side = data.get('side', 'P')
        short_strike = int(data['short_strike'])
        long_strike = int(data['long_strike'])
        limit_credit = float(data['limit_credit'])
        quantity = int(data.get('quantity', 1))
        expiry = data.get('expiry', '')
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({'status': 'error', 'error': str(exc)}), 400

    result, code = dispatch_manual_place(
        ROOT,
        launcher_active=_launcher_active(),
        side=side,
        short_strike=short_strike,
        long_strike=long_strike,
        limit_credit=limit_credit,
        quantity=quantity,
        expiry=expiry,
        structure_type=str(data.get('structure_type') or ''),
        wait_for_launcher_sec=2.0,
        **{k: data.get(k) for k in (
            'stop_loss_pct', 'stop_multiplier', 'profit_target_pct', 'on_unfilled', 'fill_wait_sec',
            'chase_floor', 'chase_max_attempts', 'max_attempts',
        ) if k in data},
    )
    return jsonify(result), code


@app.route('/api/session/row/stop', methods=['POST'])
def api_session_row_stop():
    """Q6: update session CSV stop + trade JSON + stop_update command file."""
    from blocks.entry.handoff import apply_stop_snapshot
    from blocks.session.manual_place import is_debit_side_or_structure, normalize_pct_ratio
    from blocks.stop import state as state_mod
    from blocks.stop.stop_math import sl_pct_from_stop_multiplier, stop_multiplier_from_sl_pct

    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    slot_key = data.get('slot_key')
    stop_multiplier = data.get('stop_multiplier')
    stop_loss_pct = data.get('stop_loss_pct')
    if not slot_key or (stop_multiplier is None and stop_loss_pct is None):
        return jsonify({'error': 'slot_key and stop_multiplier (or stop_loss_pct) required'}), 400

    plan = _meic_session_rows() if strategy == trades_layout.STRATEGY_MEIC else load_manual_session_today(ROOT)
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404
    row = plan.row_by_slot_key(slot_key)
    if row is None:
        return jsonify({'error': f'unknown slot_key: {slot_key}'}), 404

    is_debit = is_debit_side_or_structure(getattr(row, 'side', ''), getattr(row, 'structure_type', ''))
    spread_type = 'debit' if is_debit else 'credit'
    if stop_loss_pct is not None:
        sl_ratio = normalize_pct_ratio(stop_loss_pct, default=0.50 if is_debit else 1.0)
        stop_multiplier = stop_multiplier_from_sl_pct(sl_ratio, spread_type=spread_type)
    else:
        val = float(stop_multiplier)
        if val > 10.0:
            sl_ratio = normalize_pct_ratio(val, default=0.50 if is_debit else 1.0)
            stop_multiplier = stop_multiplier_from_sl_pct(sl_ratio, spread_type=spread_type)
        else:
            stop_multiplier = val
            sl_ratio = sl_pct_from_stop_multiplier(stop_multiplier, spread_type=spread_type)

    updates: Dict[str, Any] = {'stop_multiplier': float(stop_multiplier)}
    if 'profit_target_pct' in data and data['profit_target_pct'] is not None:
        updates['profit_target_pct'] = normalize_pct_ratio(
            data['profit_target_pct'],
            default=1.00 if is_debit else 0.50,
        )

    plan.update_row(slot_key, **updates)
    plan.save()

    trade_path = row.trade_path
    if not trade_path or not os.path.isfile(trade_path):
        return jsonify({'status': 'ok', 'row': plan.row_by_slot_key(slot_key).to_dict(), 'trade_updated': False})

    state = state_mod.load_state(trade_path)
    row = plan.row_by_slot_key(slot_key)
    apply_stop_snapshot(state, row)
    state_mod.save_state(trade_path, state)

    cmd_dir = commands_dir_for_filename(os.path.basename(trade_path), TRADES_DIR)
    cmd_path = os.path.join(cmd_dir, f'{os.path.basename(trade_path)}.stop_update.json')
    os.makedirs(cmd_dir, exist_ok=True)
    cmd_payload: Dict[str, Any] = {
        'stop_multiplier': float(stop_multiplier),
        'stop_loss_pct': float(sl_ratio),
        'stop_mode': data.get('stop_mode', 'multiplier'),
        'ts': _central_now_str(),
        'source': 'dashboard',
    }
    if 'profit_target_pct' in updates:
        cmd_payload['profit_target_pct'] = updates['profit_target_pct']
    with open(cmd_path, 'w', encoding='utf-8') as f:
        json.dump(cmd_payload, f)

    return jsonify({
        'status': 'ok',
        'row': row.to_dict(),
        'trade_updated': True,
        'command': cmd_path,
    })


@app.route('/api/session/row/pt', methods=['POST'])
def api_session_row_pt():
    """Update session CSV profit target % + trade JSON + stop_update command file."""
    from blocks.entry.handoff import apply_stop_snapshot
    from blocks.session.manual_place import normalize_pct_ratio
    from blocks.stop import state as state_mod

    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    slot_key = data.get('slot_key')
    profit_target_pct = data.get('profit_target_pct')
    if not slot_key or profit_target_pct is None:
        return jsonify({'error': 'slot_key and profit_target_pct required'}), 400

    plan = _meic_session_rows() if strategy == trades_layout.STRATEGY_MEIC else load_manual_session_today(ROOT)
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404
    row = plan.row_by_slot_key(slot_key)
    if row is None:
        return jsonify({'error': f'unknown slot_key: {slot_key}'}), 404

    pt_val = normalize_pct_ratio(profit_target_pct, default=0.50)

    plan.update_row(slot_key, profit_target_pct=pt_val)
    plan.save()

    trade_path = row.trade_path
    if not trade_path or not os.path.isfile(trade_path):
        return jsonify({'status': 'ok', 'row': plan.row_by_slot_key(slot_key).to_dict(), 'trade_updated': False})

    state = state_mod.load_state(trade_path)
    state['profit_target_pct'] = pt_val
    if 'exit_config' not in state or not isinstance(state['exit_config'], dict):
        state['exit_config'] = {}
    state['exit_config']['profit_target_pct'] = pt_val
    if isinstance(state.get('plan'), dict):
        state['plan']['profit_target_pct'] = pt_val
    state_mod.save_state(trade_path, state)

    cmd_dir = commands_dir_for_filename(os.path.basename(trade_path), TRADES_DIR)
    cmd_path = os.path.join(cmd_dir, f'{os.path.basename(trade_path)}.stop_update.json')
    os.makedirs(cmd_dir, exist_ok=True)
    with open(cmd_path, 'w', encoding='utf-8') as f:
        json.dump({
            'profit_target_pct': pt_val,
            'ts': _central_now_str(),
            'source': 'dashboard',
        }, f)

    return jsonify({
        'status': 'ok',
        'row': plan.row_by_slot_key(slot_key).to_dict(),
        'trade_updated': True,
        'command': cmd_path,
    })


@app.route('/api/regen_token', methods=['POST'])
def regen_token():
    proc = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, 'common', 'auth', 'generate_token.py')],
        cwd=os.path.join(ROOT, 'common', 'auth')
    )
    return jsonify({'status': 'token_gen_started', 'pid': proc.pid,
                    'note': 'Check the terminal that opened — paste the redirect URL there.'})

# ── History / DB routes ───────────────────────────────────────────────────────

_last_history_sync_time = 0.0
_history_sync_lock = threading.Lock()

def _maybe_sync_history(root: str, min_interval_sec: float = 60.0):
    global _last_history_sync_time
    now = time.time()
    if now - _last_history_sync_time < min_interval_sec:
        return
    if not _history_sync_lock.acquire(blocking=False):
        return
    try:
        if now - _last_history_sync_time < min_interval_sec:
            return
        from dashboard.history_sync import sync_history_from_disk
        sync_history_from_disk(root)
        _last_history_sync_time = time.time()
    except Exception as e:
        log.warning("Background history sync error: %s", e)
    finally:
        _history_sync_lock.release()

@app.route('/api/history')
def api_history():
    _maybe_sync_history(ROOT)
    flags = _history_money_flags()
    date_val = request.args.get('date') or None
    strategy = request.args.get('strategy') or None
    limit = int(request.args.get('limit', 200))
    return jsonify(get_trades(
        date=date_val,
        strategy=strategy,
        limit=limit,
        **flags,
    ))


@app.route('/api/history/sync', methods=['POST'])
def api_history_sync():
    from dashboard.history_sync import sync_history_from_disk
    from common.expiry_settlement import write_spx_settlement_close

    data = request.get_json(silent=True) or {}
    spx_close = data.get('spx_close')
    for_date = data.get('date')
    if spx_close is not None and for_date:
        write_spx_settlement_close(
            date.fromisoformat(for_date),
            float(spx_close),
            root=ROOT,
            source='operator_manual',
            locked=True,
            note='Operator-confirmed 3 PM CT SPX cash close',
        )
    result = sync_history_from_disk(ROOT)
    return jsonify(result)


@app.route('/api/history/pnl_csv')
def api_history_pnl_csv():
    """Download consolidated strategy daily P&L spreadsheet as CSV."""
    from dashboard.strategy_pnl_csv import export_strategy_daily_pnl_csv
    csv_path = export_strategy_daily_pnl_csv(ROOT)
    with open(csv_path, 'r', encoding='utf-8') as f:
        content = f.read()
    return Response(
        content,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=strategy_daily_pnl.csv'},
    )


@app.route('/api/history/daily_strategy_summary')
def api_history_daily_strategy_summary():
    """Return lightweight daily summary grouped across all strategies."""
    from dashboard.strategy_pnl_csv import build_daily_strategy_summary
    return jsonify(build_daily_strategy_summary(ROOT))


@app.route('/api/daily_summary')
def api_daily_summary():
    _maybe_sync_history(ROOT)
    days = int(request.args.get('days', 30))
    strategy = request.args.get('strategy') or None
    return jsonify(get_daily_summary(days=days, strategy=strategy))

@app.route('/api/stats')
def api_stats():
    _maybe_sync_history(ROOT)
    flags = _history_money_flags()
    strategy = request.args.get('strategy') or None
    if strategy:
        return jsonify(get_stats(strategy=strategy, **flags))
    payload = get_stats_by_strategy(**flags)
    payload['total_pnl'] = payload['all']['total_pnl']
    payload['total_trades'] = payload['all']['total_trades']
    payload['win_rate'] = payload['all']['win_rate']
    return jsonify(payload)

@app.route('/api/daily_calendar')
def api_daily_calendar():
    _maybe_sync_history(ROOT)
    flags = _history_money_flags()
    days = int(request.args.get('days', 31))
    strategy = request.args.get('strategy') or None
    rows = get_daily_breakdown(days=days, **flags)
    strategy_keys = {
        STRATEGY_MEIC: ('meic_pnl', 'meic_trades', 'meic_wins'),
        STRATEGY_MANUAL: ('manual_pnl', 'manual_trades', 'manual_wins'),
        STRATEGY_SKYLINE: ('skyline_pnl', 'skyline_trades', 'skyline_wins'),
        STRATEGY_PERMA_BULL: ('perma_bull_pnl', 'perma_bull_trades', 'perma_bull_wins'),
        STRATEGY_MEAN_REVERSION: ('mean_reversion_pnl', 'mean_reversion_trades', 'mean_reversion_wins'),
        STRATEGY_IRON_FLY: ('iron_fly_pnl', 'iron_fly_trades', 'iron_fly_wins'),
        STRATEGY_IRON_FLY_XSP: ('iron_fly_xsp_pnl', 'iron_fly_xsp_trades', 'iron_fly_xsp_wins'),
        STRATEGY_DRMA_HM: ('drma_hm_pnl', 'drma_hm_trades', 'drma_hm_wins'),
        STRATEGY_DRMA_HM_DIR: ('drma_dir_pnl', 'drma_dir_trades', 'drma_dir_wins'),
        STRATEGY_DRMA_HM_NONDIR: ('drma_nondir_pnl', 'drma_nondir_trades', 'drma_nondir_wins'),
        STRATEGY_DRMA_HM_FUT: ('drma_fut_pnl', 'drma_fut_trades', 'drma_fut_wins'),
        STRATEGY_DRMA_STOCKS: ('drma_stocks_pnl', 'drma_stocks_trades', 'drma_stocks_wins'),
        STRATEGY_DRMA_STOCK_DIR: ('drma_stock_dir_pnl', 'drma_stock_dir_trades', 'drma_stock_dir_wins'),
        STRATEGY_DRMA_STOCK_NONDIR: ('drma_stock_nondir_pnl', 'drma_stock_nondir_trades', 'drma_stock_nondir_wins'),
        STRATEGY_DRMA_STOCK_EQUITY: ('drma_stock_equity_pnl', 'drma_stock_equity_trades', 'drma_stock_equity_wins'),
        STRATEGY_INVERSE_MEIC: ('inverse_meic_pnl', 'inverse_meic_trades', 'inverse_meic_wins'),
    }
    if strategy:
        pnl_key, trade_key, win_key = strategy_keys.get(
            strategy, ('pnl', 'numTrades', 'num_wins'),
        )
        filtered = []
        for row in rows:
            n = row.get(trade_key, 0)
            filtered.append({
                'date': row['date'],
                'pnl': row.get(pnl_key, 0),
                'winRate': round((row.get(win_key, 0) / n * 100) if n else 0, 0),
                'numTrades': n,
                'meic_trades': row.get('meic_trades', 0),
                'manual_trades': row.get('manual_trades', 0),
                'skyline_trades': row.get('skyline_trades', 0),
                'perma_bull_trades': row.get('perma_bull_trades', 0),
                'mean_reversion_trades': row.get('mean_reversion_trades', 0),
                'iron_fly_trades': row.get('iron_fly_trades', 0),
                'iron_fly_xsp_trades': row.get('iron_fly_xsp_trades', 0),
                'drma_hm_trades': row.get('drma_hm_trades', 0),
                'drma_dir_trades': row.get('drma_dir_trades', 0),
                'drma_nondir_trades': row.get('drma_nondir_trades', 0),
                'drma_fut_trades': row.get('drma_fut_trades', 0),
                'meic_pnl': row.get('meic_pnl', 0),
                'manual_pnl': row.get('manual_pnl', 0),
                'skyline_pnl': row.get('skyline_pnl', 0),
                'perma_bull_pnl': row.get('perma_bull_pnl', 0),
                'mean_reversion_pnl': row.get('mean_reversion_pnl', 0),
                'iron_fly_pnl': row.get('iron_fly_pnl', 0),
                'iron_fly_xsp_pnl': row.get('iron_fly_xsp_pnl', 0),
                'drma_hm_pnl': row.get('drma_hm_pnl', 0),
                'drma_dir_pnl': row.get('drma_dir_pnl', 0),
                'drma_nondir_pnl': row.get('drma_nondir_pnl', 0),
                'drma_fut_pnl': row.get('drma_fut_pnl', 0),
            })
        return jsonify(filtered)

    result = []
    for row in rows:
        result.append({
            'date': row['date'],
            'pnl': row['pnl'],
            'winRate': row['winRate'],
            'numTrades': row['numTrades'],
            'meic_pnl': row.get('meic_pnl', 0),
            'manual_pnl': row.get('manual_pnl', 0),
            'skyline_pnl': row.get('skyline_pnl', 0),
            'perma_bull_pnl': row.get('perma_bull_pnl', 0),
            'mean_reversion_pnl': row.get('mean_reversion_pnl', 0),
            'iron_fly_pnl': row.get('iron_fly_pnl', 0),
            'iron_fly_xsp_pnl': row.get('iron_fly_xsp_pnl', 0),
            'drma_hm_pnl': row.get('drma_hm_pnl', 0),
            'drma_dir_pnl': row.get('drma_dir_pnl', 0),
            'drma_nondir_pnl': row.get('drma_nondir_pnl', 0),
            'drma_fut_pnl': row.get('drma_fut_pnl', 0),
            'meic_trades': row.get('meic_trades', 0),
            'manual_trades': row.get('manual_trades', 0),
            'skyline_trades': row.get('skyline_trades', 0),
            'perma_bull_trades': row.get('perma_bull_trades', 0),
            'mean_reversion_trades': row.get('mean_reversion_trades', 0),
            'iron_fly_trades': row.get('iron_fly_trades', 0),
            'iron_fly_xsp_trades': row.get('iron_fly_xsp_trades', 0),
            'drma_hm_trades': row.get('drma_hm_trades', 0),
            'drma_dir_trades': row.get('drma_dir_trades', 0),
            'drma_nondir_trades': row.get('drma_nondir_trades', 0),
            'drma_fut_trades': row.get('drma_fut_trades', 0),
        })
    return jsonify(result)

@app.route('/api/trade/<int:trade_id>', methods=['DELETE'])
def api_delete_trade(trade_id):
    delete_trade(trade_id)
    return jsonify({'status': 'deleted', 'id': trade_id})


@app.route('/api/skyline/today')
def api_skyline_today():
    """Read-only Skyline status for today's trade."""
    import glob
    from strategies.skyline.settlement import attach_dollar_pnl_display, update_unrealized_marks
    from common.session_cleanup import central_today

    paper = _strategy_paper_flag(trades_layout.STRATEGY_SKYLINE)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_SKYLINE, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')
    trade_path = os.path.join(active, f'skyline_{today}.json')
    history_trade_path = os.path.join(hist_dir, f'skyline_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    trade = None
    trade_source = None
    for candidate_path in (trade_path, history_trade_path):
        if not os.path.isfile(candidate_path):
            continue
        try:
            with open(candidate_path, encoding='utf-8') as f:
                trade = json.load(f)
            trade_source = candidate_path
            break
        except (OSError, json.JSONDecodeError):
            trade = None

    if trade:
        if trade.get('status') == 'open' and trade_source == trade_path:
            symbols = []
            for struct in trade.get('structures') or []:
                for leg in struct.get('legs') or []:
                    sym = leg.get('symbol')
                    if sym:
                        symbols.append(sym)
            if symbols:
                try:
                    from common.broker_factory import get_shared_broker_for_strategy
                    broker = get_shared_broker_for_strategy(
                        trades_layout.STRATEGY_SKYLINE, paper=paper,
                    )
                    mids = broker.fetch_option_mids_api(symbols)
                    update_unrealized_marks(trade, mids)
                except Exception:
                    pass
        attach_dollar_pnl_display(trade)

    return jsonify({
        'strategy': trades_layout.STRATEGY_SKYLINE,
        'paper': paper,
        'date': today,
        'session': session,
        'trade': trade,
        'history': [],
    })


@app.route('/api/perma_bull/today')
def api_perma_bull_today():
    """Read-only Perma Bull PCS status for today's trade."""
    from common.session_cleanup import central_today
    from common.expiry_settlement import get_spx_settlement_close

    paper = _strategy_paper_flag(trades_layout.STRATEGY_PERMA_BULL)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_PERMA_BULL, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    spx_settle = get_spx_settlement_close(central_today(), root=ROOT)
    trade = _perma_bull_trade_for_date(today)
    if trade:
        trade['live_pnl_dollars'] = _handshake_trade_live_pnl(trade, spx_settle=spx_settle)
        trade['entry_time'] = trade_entry_time_iso(trade)
        trade['exit_time'] = trade_exit_time_iso(trade)
        trade['profit_target_debit'] = float(
            (trade.get('exit_config') or {}).get('profit_target_debit')
            or trade.get('profit_target_debit')
            or 0.15
        )
        trade['quantity'] = int(trade.get('quantity') or trade.get('filled_quantity') or 1)
        trade['filename'] = os.path.basename(trade.get('_filepath', ''))

    return jsonify({
        'strategy': trades_layout.STRATEGY_PERMA_BULL,
        'paper': paper,
        'date': today,
        'session': session,
        'trade': trade,
    })


@app.route('/api/perma_bull/close', methods=['POST'])
def api_perma_bull_close():
    """Manual close command for today's active Perma Bull PCS trade."""
    from common.session_cleanup import central_today
    from common.trades_layout import active_dir_for_strategy, STRATEGY_PERMA_BULL
    from blocks.stop import state as state_mod

    today = central_today().isoformat()
    trade = _perma_bull_trade_for_date(today)
    if not trade:
        return jsonify({'status': 'error', 'error': 'No Perma Bull trade found for today'}), 404

    if str(trade.get('status', '')).lower() == 'closed':
        return jsonify({'status': 'error', 'error': 'Trade is already closed'}), 400

    filename = os.path.basename(trade.get('_filepath', ''))
    if not filename:
        active = active_dir_for_strategy(STRATEGY_PERMA_BULL, ROOT)
        for path in glob.glob(os.path.join(active, '*.json')):
            if not os.path.basename(path).startswith(('session_', 'halted_')):
                filename = os.path.basename(path)
                break

    if not filename:
        return jsonify({'status': 'error', 'error': 'Could not identify active trade filename'}), 404

    cmd_dir = os.path.join(ROOT, 'trades', 'commands')
    os.makedirs(cmd_dir, exist_ok=True)
    cmd_path = os.path.join(cmd_dir, f'{filename}.close.json')
    try:
        with open(cmd_path, 'w', encoding='utf-8') as f:
            json.dump({
                'action': 'close',
                'close_mechanism': 'manual_close',
                'ts': state_mod.now_iso(),
                'source': 'dashboard_perma_bull',
            }, f)
        log.info('Wrote manual close command for Perma Bull %s at %s', filename, cmd_path)
    except Exception as exc:
        log.error('Failed writing close command for %s: %s', filename, exc)
        return jsonify({'status': 'error', 'error': str(exc)}), 500

    emit_summary_update()
    return jsonify({
        'status': 'ok',
        'message': f'Close command issued for {filename}',
        'filename': filename,
    })


@app.route('/api/perma_bull/manage_manually', methods=['POST'])
def api_perma_bull_manage_manually():
    """Toggle manual management for today's active Perma Bull PCS trade.
    Disarms resting OCO/stop order on the broker so operator can manually manage, close, or roll.
    """
    from common.session_cleanup import central_today
    from common.trades_layout import active_dir_for_strategy, STRATEGY_PERMA_BULL
    from blocks.stop import state as state_mod

    today = central_today().isoformat()
    trade = _perma_bull_trade_for_date(today)
    if not trade:
        return jsonify({'status': 'error', 'error': 'No Perma Bull trade found for today'}), 404

    if str(trade.get('status', '')).lower() == 'closed':
        return jsonify({'status': 'error', 'error': 'Trade is already closed'}), 400

    filename = os.path.basename(trade.get('_filepath', ''))
    if not filename:
        active = active_dir_for_strategy(STRATEGY_PERMA_BULL, ROOT)
        for p in glob.glob(os.path.join(active, '*.json')):
            if not os.path.basename(p).startswith(('session_', 'halted_')):
                filename = os.path.basename(p)
                trade['_filepath'] = p
                break

    if not filename:
        return jsonify({'status': 'error', 'error': 'Could not identify active trade filename'}), 404

    payload = request.get_json(silent=True) or {}
    currently_manual = bool(trade.get('manual_management'))
    enabled = payload.get('enabled', not currently_manual if 'enabled' not in payload else payload['enabled'])

    # Write command file for StopMonitor
    cmd_dir = os.path.join(ROOT, 'trades', 'commands')
    os.makedirs(cmd_dir, exist_ok=True)
    cmd_path = os.path.join(cmd_dir, f'{filename}.manage_manually.json')
    try:
        with open(cmd_path, 'w', encoding='utf-8') as f:
            json.dump({
                'action': 'manage_manually',
                'enabled': bool(enabled),
                'ts': state_mod.now_iso(),
                'source': 'dashboard_perma_bull',
            }, f)
        log.info('Wrote manage_manually command (enabled=%s) for Perma Bull %s at %s', enabled, filename, cmd_path)
    except Exception as exc:
        log.error('Failed writing manage_manually command for %s: %s', filename, exc)
        return jsonify({'status': 'error', 'error': str(exc)}), 500

    # Also directly cancel active stop on broker if enabling manual management
    active_stop = trade.get('active_stop') or {}
    order_id = active_stop.get('order_id')
    if enabled and order_id:
        try:
            from common.broker_factory import get_shared_broker_for_strategy
            paper = _strategy_paper_flag(trades_layout.STRATEGY_PERMA_BULL)
            broker = get_shared_broker_for_strategy(trades_layout.STRATEGY_PERMA_BULL, paper=paper)
            if broker and hasattr(broker, 'cancel_order'):
                broker.cancel_order(str(order_id))
                log.info('Directly cancelled stop/OCO #%s on broker for manual management', order_id)
        except Exception as exc:
            log.warning('Direct broker cancel failed in server (command file will handle): %s', exc)

    # Immediately reflect in trade file
    trade_path = trade.get('_filepath')
    if trade_path and os.path.isfile(trade_path):
        try:
            from blocks.stop import state as state_mod
            st = state_mod.load_state(trade_path)
            st['manual_management'] = bool(enabled)
            if enabled:
                st['active_stop'] = None
                st['stop_quantity'] = 0
                st['status_reason'] = 'Managed manually by user (stop/OCO disarmed)'
                if order_id:
                    state_mod.append_stop_history(
                        st,
                        action='cancelled',
                        order_id=order_id,
                        price=float((active_stop or {}).get('stop_price') or 0.0),
                        phase=int((active_stop or {}).get('phase', 1)),
                        reason='manage_manually',
                    )
            else:
                st['status_reason'] = 'Automated management re-armed'
            state_mod.save_state(trade_path, st)
        except Exception as exc:
            log.warning('Could not update trade file directly: %s', exc)

    emit_summary_update()
    msg = (
        'Perma Bull switched to Manual Management. Stop/OCO disarmed on broker.'
        if enabled
        else 'Automated management re-armed for Perma Bull.'
    )
    return jsonify({
        'status': 'ok',
        'manual_management': bool(enabled),
        'message': msg,
    })


@app.route('/api/perma_bull/params', methods=['POST'])
def api_perma_bull_params():
    """Update Perma Bull strategy parameters (quantity, spread_width, target_credit, profit_target_debit)."""
    data = request.get_json(force=True) or {}
    from common.session_cleanup import central_today
    from common.daily_overrides import set_overrides
    from strategies.perma_bull_pcs.strategy import PermaBullPCSConfig

    updates = {}
    if 'quantity' in data:
        try:
            qty = int(data['quantity'])
            if qty > 0:
                updates['quantity'] = qty
        except (ValueError, TypeError):
            pass

    if 'spread_width' in data:
        try:
            sw = int(data['spread_width'])
            if sw > 0:
                updates['spread_width'] = sw
        except (ValueError, TypeError):
            pass

    if 'target_credit' in data:
        try:
            tc = round(float(data['target_credit']), 2)
            if tc > 0:
                updates['target_credit'] = tc
        except (ValueError, TypeError):
            pass

    if 'profit_target_debit' in data:
        try:
            pt = round(float(data['profit_target_debit']), 2)
            if pt > 0:
                updates['profit_target_debit'] = pt
        except (ValueError, TypeError):
            pass

    if not updates:
        return jsonify({'status': 'error', 'error': 'No valid parameters provided'}), 400

    # 1. Update strategy overrides (persisted to session & strategy_overrides.json)
    try:
        set_overrides('PERMA_BULL_PCS', ROOT, updates, PermaBullPCSConfig)
    except Exception as exc:
        log.warning('Could not set overrides for PERMA_BULL_PCS: %s', exc)

    # 2. If an active trade is currently open, dynamically update its exit_config
    today = central_today().isoformat()
    trade = _perma_bull_trade_for_date(today)
    updated_active = False
    if trade and str(trade.get('status', '')).lower() != 'closed' and trade.get('_filepath'):
        filepath = trade['_filepath']
        try:
            from blocks.stop import state as state_mod
            st = state_mod.load_state(filepath)
            if 'profit_target_debit' in updates:
                exit_cfg = st.setdefault('exit_config', {})
                exit_cfg['profit_target_debit'] = updates['profit_target_debit']
                st['profit_target_debit'] = updates['profit_target_debit']
            state_mod.save_state(filepath, st)
            updated_active = True
            log.info('Updated active trade %s exit_config with %s', filepath, updates)
        except Exception as exc:
            log.warning('Could not update active trade JSON %s: %s', filepath, exc)

    emit_summary_update()
    return jsonify({
        'status': 'ok',
        'updates': updates,
        'updated_active_trade': updated_active,
    })


@app.route('/api/inverse_meic/today')
def api_inverse_meic_today():
    """Read-only Inverse MEIC status for today's market regime and trades."""
    from common.session_cleanup import central_today
    from strategies.inverse_meic.strategy import InverseMEICStrategy
    from strategies.inverse_meic.config import InverseMEICConfig

    paper = _strategy_paper_flag(trades_layout.STRATEGY_INVERSE_MEIC)
    today = central_today().isoformat()

    strat = InverseMEICStrategy(InverseMEICConfig(paper=paper, root=ROOT))
    
    # Check for manual regime override query param
    overrides = {}
    force_armed_arg = request.args.get('force_armed')
    if force_armed_arg is not None:
        overrides['force_armed'] = force_armed_arg.lower() in ('1', 'true', 'yes')

    if overrides:
        strat.evaluate_regime(overrides=overrides)

    status = strat.get_status()
    status['paper'] = paper
    status['date'] = today

    return jsonify(status)


@app.route('/api/mean_reversion/today')
def api_mean_reversion_today():
    """Read-only Mean Reversion status for today's trade."""
    import glob
    from strategies.mean_reversion.settlement import attach_dollar_pnl_display, update_unrealized_marks
    from common.session_cleanup import central_today

    paper = _strategy_paper_flag(trades_layout.STRATEGY_MEAN_REVERSION)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_MEAN_REVERSION, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_MEAN_REVERSION, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')
    trade_path = os.path.join(active, f'mean_reversion_{today}.json')
    history_trade_path = os.path.join(hist_dir, f'mean_reversion_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    trade = None
    trade_source = None
    for candidate_path in (trade_path, history_trade_path):
        if not os.path.isfile(candidate_path):
            continue
        try:
            with open(candidate_path, encoding='utf-8') as f:
                trade = json.load(f)
            trade_source = candidate_path
            break
        except (OSError, json.JSONDecodeError):
            trade = None

    if trade:
        if trade.get('status') == 'open' and trade_source == trade_path:
            symbols = []
            for struct in trade.get('structures') or []:
                for leg in struct.get('legs') or []:
                    sym = leg.get('symbol')
                    if sym:
                        symbols.append(sym)
            if symbols:
                try:
                    from common.broker_factory import get_shared_broker_for_strategy
                    broker = get_shared_broker_for_strategy(
                        trades_layout.STRATEGY_MEAN_REVERSION, paper=paper,
                    )
                    mids = broker.fetch_option_mids_api(symbols)
                    update_unrealized_marks(trade, mids)
                except Exception:
                    pass
        attach_dollar_pnl_display(trade)

    return jsonify({
        'strategy': trades_layout.STRATEGY_MEAN_REVERSION,
        'paper': paper,
        'date': today,
        'session': session,
        'trade': trade,
        'history': [],
    })


@app.route('/api/drma_hm/today')
def api_drma_hm_today():
    """Read-only DRMA + Hilega-Milega status for today's trades and indicators."""
    import glob
    from strategies.drma_hm.market_data_feed import fetch_es_current_price
    from strategies.drma_hm.settlement import (
        attach_dollar_pnl_display,
        update_futures_unrealized_marks,
        update_unrealized_marks,
    )
    from common.session_cleanup import central_today

    paper = _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM)
    dir_paper = _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM_DIR)
    non_dir_paper = _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM_NONDIR)
    futures_paper = _strategy_paper_flag(trades_layout.STRATEGY_DRMA_HM_FUT)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    # Check running strategy status if available in memory
    strat_status = None
    try:
        from strategies.loader import load_strategies
        for strat, cfg in load_strategies():
            if cfg.name == trades_layout.STRATEGY_DRMA_HM and hasattr(strat, 'get_status'):
                strat_status = strat.get_status()
                break
    except Exception:
        pass

    history = []
    hist_trade_ids = set()
    for path in _collect_drma_hm_session_trade_files(hist_dir, today):
        try:
            with open(path, encoding='utf-8') as f:
                row = json.load(f)
            attach_dollar_pnl_display(row)
            if not row.get('date'):
                entered_at = row.get('entered_at') or ''
                row['date'] = entered_at[:10] if len(entered_at) >= 10 else today
            history.append(row)
            if row.get('trade_id'):
                hist_trade_ids.add(row['trade_id'])
        except (OSError, json.JSONDecodeError):
            continue

    # Sort history descending by entry time (newest on top)
    history.sort(key=lambda x: str(x.get('entered_at') or x.get('date') or ''), reverse=True)

    # Find active trades (skip and prune any that are already in history or marked closed)
    trades = []
    trade = None
    live_es_price = fetch_es_current_price(levels=session.get('levels') or {}, spx_spot=(strat_status.get('close') if strat_status else None))

    if os.path.isdir(active):
        candidates = _collect_drma_hm_session_trade_files(active, today)
        all_symbols = []
        open_trades_with_paths = []
        for path in candidates:
            try:
                with open(path, encoding='utf-8') as f:
                    t = json.load(f)
                tid = t.get('trade_id')
                st = t.get('status')
                # If active trade has a broker OCO order, check if broker already filled it
                if st == 'open' and t.get('oco_order_id') and t.get('paper') is False:
                    try:
                        from common.broker_factory import get_shared_broker_for_strategy
                        b_chk = get_shared_broker_for_strategy(trades_layout.STRATEGY_DRMA_HM, paper=False)
                        if hasattr(b_chk, 'get_order_status'):
                            chk_st = b_chk.get_order_status(str(t['oco_order_id']))
                            if chk_st and getattr(chk_st, 'status', '').lower() == 'filled':
                                from strategies.drma_hm.strategy import DRMAHMStrategy
                                DRMAHMStrategy()._manage_open_trade(ROOT, path)
                                try:
                                    with open(path, encoding='utf-8') as f_re:
                                        t = json.load(f_re)
                                    st = t.get('status')
                                except (OSError, json.JSONDecodeError):
                                    st = 'closed'
                    except Exception:
                        pass

                # If already in history or marked closed, clean up stale active file
                if (tid and tid in hist_trade_ids) or st == 'closed':
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                trades.append(t)
                if st in ('open', 'closing'):
                    open_trades_with_paths.append((t, path))
                    is_fut = (t.get('structure_type') == 'futures' or t.get('trade_type') == 'DIRECTIONAL_FUTURES')
                    if is_fut:
                        if live_es_price:
                            update_futures_unrealized_marks(t, live_es_price)
                            attach_dollar_pnl_display(t)
                            try:
                                with open(path, 'w', encoding='utf-8') as out_f:
                                    json.dump(t, out_f, indent=2)
                            except Exception:
                                pass
                    else:
                        for struct in t.get('structures') or []:
                            for leg in struct.get('legs') or []:
                                sym = leg.get('symbol')
                                if sym and sym not in all_symbols and not sym.startswith(('ES', '/ES')):
                                    all_symbols.append(sym)
            except (OSError, json.JSONDecodeError):
                continue

        if all_symbols and open_trades_with_paths:
            try:
                from common.broker_factory import get_shared_broker_for_strategy
                broker = get_shared_broker_for_strategy(trades_layout.STRATEGY_DRMA_HM, paper=paper)
                if hasattr(broker, 'fetch_option_mids_api'):
                    mids = broker.fetch_option_mids_api(all_symbols)
                    for t, p in open_trades_with_paths:
                        if t.get('structure_type') != 'futures' and t.get('trade_type') != 'DIRECTIONAL_FUTURES':
                            update_unrealized_marks(t, mids)
                            attach_dollar_pnl_display(t)
                            try:
                                with open(p, 'w', encoding='utf-8') as out_f:
                                    json.dump(t, out_f, indent=2)
                            except Exception:
                                pass
            except Exception:
                pass

        for t in trades:
            attach_dollar_pnl_display(t)

        # Sort active trades descending by entry time
        trades.sort(key=lambda x: str(x.get('entered_at') or x.get('date') or ''), reverse=True)
        trade = trades[0] if trades else None

    live_realized_pnl = sum(float(h.get('realized_pnl_dollars') or 0.0) for h in history if h.get('paper') is False)
    paper_realized_pnl = sum(float(h.get('realized_pnl_dollars') or 0.0) for h in history if h.get('paper') is not False)

    live_open_pnl = sum(float(t.get('unrealized_pnl_dollars') or 0.0) for t in trades if t.get('status') in ('open', 'closing') and t.get('paper') is False)
    paper_open_pnl = sum(float(t.get('unrealized_pnl_dollars') or 0.0) for t in trades if t.get('status') in ('open', 'closing') and t.get('paper') is not False)

    live_total_pnl = round(live_realized_pnl + live_open_pnl, 2)
    paper_total_pnl = round(paper_realized_pnl + paper_open_pnl, 2)
    realized_pnl = round(live_realized_pnl + paper_realized_pnl, 2)
    open_pnl = round(live_open_pnl + paper_open_pnl, 2)
    total_pnl = round(realized_pnl + open_pnl, 2)

    # Resolve indicators & levels
    indicators = None
    levels = session.get('levels') or {}
    if strat_status and strat_status.get('close'):
        indicators = strat_status
    elif session.get('indicators'):
        indicators = session.get('indicators')

    today_date = central_today()
    data_root = os.path.join(ROOT, 'data')

    # Resolve strategy config and session phase
    from strategies.drma_hm.config import DRMAHMConfig
    from strategies.drma_hm.strategy import DRMAHMStrategy
    from common.daily_overrides import effective_config
    cfg = effective_config(DRMAHMConfig(name=trades_layout.STRATEGY_DRMA_HM, paper=paper), 'DRMA_HM_SPX', ROOT)
    dummy_strat = DRMAHMStrategy(config=cfg)
    current_phase = strat_status.get('session_phase') if (strat_status and strat_status.get('session_phase')) else dummy_strat.get_session_phase(_central_now())

    timing = {
        'session_phase': current_phase,
        'enable_overnight_futures': bool(getattr(cfg, 'enable_overnight_futures', True)),
        'overnight_flatten_time_cst': getattr(cfg, 'overnight_flatten_time_cst', '07:00'),
        'market_open_time_cst': getattr(cfg, 'market_open_time_cst', '08:30'),
        'regular_trading_start_time_cst': getattr(cfg, 'regular_trading_start_time_cst', '09:30'),
        'eod_exit_time_cst': getattr(cfg, 'eod_exit_time_cst', '14:50'),
    }

    # Ensure reference levels are available
    if not levels or levels.get('pdh') is None:
        try:
            from strategies.drma_hm.market_data_feed import fetch_yahoo_market_levels
            raw_levels = fetch_yahoo_market_levels(today_date, cfg, ROOT, allow_yahoo=False)
            if raw_levels:
                levels = {
                    'pdh': raw_levels.get('pdh'),
                    'pdl': raw_levels.get('pdl'),
                    'pdc': raw_levels.get('pdc'),
                    'onh': raw_levels.get('onh'),
                    'onl': raw_levels.get('onl'),
                    'es_basis': raw_levels.get('es_offset'),
                    'es_offset': raw_levels.get('es_offset'),
                }
        except Exception as exc:
            log.warning('Could not resolve DRM-HM levels: %s', exc)

    # Ensure indicators are available (overnight ES vs daytime SPX)
    if not indicators or indicators.get('close') is None:
        try:
            if current_phase == 'OVERNIGHT_TRADING' and getattr(cfg, 'enable_overnight_futures', True):
                from strategies.drma_hm.market_data_feed import load_es_5m_bars
                from strategies.drma_hm.indicators import compute_indicators
                fut_sym = str(getattr(cfg, 'futures_symbol', 'ES') or 'ES').upper()
                df_es = load_es_5m_bars(data_root, today_date, symbol=fut_sym, include_overnight=True, allow_backfill=False)
                if not df_es.empty:
                    _, snaps = compute_indicators(
                        df_es,
                        pdh=levels.get('pdh'), pdl=levels.get('pdl'), pdc=levels.get('pdc'),
                        onh=levels.get('onh'), onl=levels.get('onl'),
                    )
                    if snaps:
                        latest = snaps[-1]
                        indicators = {
                            'last_bar': latest.datetime_str,
                            'close': latest.close,
                            'drma': latest.out_ma,
                            'orb_formed': latest.orb_formed,
                            'orb_high': latest.orb_high,
                            'orb_low': latest.orb_low,
                            'rsi': latest.rsi,
                            'fast': latest.hm_fast,
                            'slow': latest.hm_slow,
                            'bias': latest.bias,
                            'grade': latest.setup_grade,
                            'vehicle': 'DIRECTIONAL ES FUTURES (OVERNIGHT)' if latest.setup_grade == 'Grade A+' else latest.trade_vehicle,
                            'gamma_risk': latest.gamma_risk,
                            'active_score': latest.active_score,
                            'is_overnight': True,
                        }
        except Exception as exc:
            log.warning('DRMA overnight fallback indicator calculation failed: %s', exc)

    if not indicators or indicators.get('close') is None:
        try:
            from strategies.drma_hm.market_data_feed import load_spx_5m_bars
            from strategies.drma_hm.indicators import compute_indicators
            df_bars = load_spx_5m_bars(data_root, today_date, prepend_prev_bars=40)
            if df_bars.empty and os.path.isdir(data_root):
                all_dates = sorted([d for d in os.listdir(data_root) if os.path.isdir(os.path.join(data_root, d)) and d.startswith('202')])
                if all_dates:
                    try:
                        fallback_d = date.fromisoformat(all_dates[-1])
                        df_bars = load_spx_5m_bars(data_root, fallback_d, prepend_prev_bars=40)
                    except Exception:
                        pass
            if not df_bars.empty:
                _, snaps = compute_indicators(
                    df_bars,
                    pdh=levels.get('pdh'), pdl=levels.get('pdl'), pdc=levels.get('pdc'),
                    onh=levels.get('onh'), onl=levels.get('onl'),
                )
                if snaps:
                    latest = snaps[-1]
                    indicators = {
                        'last_bar': latest.datetime_str,
                        'close': latest.close,
                        'drma': latest.out_ma,
                        'orb_formed': latest.orb_formed,
                        'orb_high': latest.orb_high,
                        'orb_low': latest.orb_low,
                        'rsi': latest.rsi,
                        'fast': latest.hm_fast,
                        'slow': latest.hm_slow,
                        'bias': latest.bias,
                        'grade': latest.setup_grade,
                        'vehicle': latest.trade_vehicle,
                        'gamma_risk': latest.gamma_risk,
                        'active_score': latest.active_score,
                    }
        except Exception as exc:
            log.warning('DRMA fallback indicator calculation failed: %s', exc)

    if live_es_price:
        levels['es_price'] = live_es_price
        if indicators:
            indicators['es_price'] = live_es_price

    return jsonify({
        'strategy': trades_layout.STRATEGY_DRMA_HM,
        'paper': paper,
        'dir_paper': dir_paper,
        'non_dir_paper': non_dir_paper,
        'futures_paper': futures_paper,
        'date': today,
        'session': session,
        'strat_status': strat_status,
        'timing': timing,
        'indicators': indicators,
        'levels': levels,
        'trades': trades,
        'trade': trade,
        'history': history,
        'realized_pnl': realized_pnl,
        'open_pnl': open_pnl,
        'total_pnl': total_pnl,
        'live_realized_pnl': live_realized_pnl,
        'live_open_pnl': live_open_pnl,
        'live_total_pnl': live_total_pnl,
        'paper_realized_pnl': paper_realized_pnl,
        'paper_open_pnl': paper_open_pnl,
        'paper_total_pnl': paper_total_pnl,
    })


@app.route('/api/drma_stocks/today')
def api_drma_stocks_today():
    """Read-only and live evaluation of DRMA Stocks strategy across watchlist tickers."""
    import glob
    from common.session_cleanup import central_today
    from strategies.drma_stocks.config import DRMAStocksConfig
    from strategies.drma_stocks.strategy import DRMAStocksStrategy
    from strategies.drma_stocks.watchlist import load_watchlist

    today = central_today().isoformat()
    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
    os.makedirs(active_dir, exist_ok=True)
    os.makedirs(hist_dir, exist_ok=True)

    # Trigger evaluation/update of all stocks
    from common.daily_overrides import effective_config
    cfg = effective_config(DRMAStocksConfig(name=trades_layout.STRATEGY_DRMA_STOCKS, root=ROOT), 'DRMA_HM_STOCKS', ROOT)
    strat = DRMAStocksStrategy(config=cfg)
    stock_summaries = strat.evaluate_all_stocks(ROOT)

    # Read active open trades
    active_trades = []
    if os.path.isdir(active_dir):
        for path in sorted(glob.glob(os.path.join(active_dir, '*.json')), reverse=True):
            if os.path.basename(path).startswith('session_'):
                continue
            fname = os.path.basename(path)
            if os.path.isdir(hist_dir) and os.path.isfile(os.path.join(hist_dir, fname)):
                try:
                    os.remove(path)
                except Exception:
                    pass
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    t = json.load(f)
                if t.get('status') == 'closed':
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                    continue
                if t.get('status') in ('open', 'closing'):
                    active_trades.append(t)
            except Exception:
                continue

    # Read today's closed history
    history_trades = []
    if os.path.isdir(hist_dir):
        for path in sorted(glob.glob(os.path.join(hist_dir, '*.json')), reverse=True):
            try:
                with open(path, encoding='utf-8') as f:
                    t = json.load(f)
                closed_at = t.get('closed_at') or t.get('entered_at') or ''
                if closed_at.startswith(today):
                    history_trades.append(t)
            except Exception:
                continue

    realized_pnl = sum(float(t.get('realized_pnl_dollars') or 0.0) for t in history_trades)
    open_pnl = sum(float(t.get('unrealized_pnl_dollars') or 0.0) for t in active_trades)
    total_pnl = round(realized_pnl + open_pnl, 2)

    stocks_list = list(stock_summaries.values()) if isinstance(stock_summaries, dict) else stock_summaries
    return jsonify({
        'strategy': trades_layout.STRATEGY_DRMA_STOCKS,
        'paper': True,
        'date': today,
        'watchlist': load_watchlist(ROOT),
        'stocks': stocks_list,
        'stocks_by_ticker': stock_summaries if isinstance(stock_summaries, dict) else {s.get('ticker', s.get('symbol')): s for s in stock_summaries},
        'active_trades': active_trades,
        'history_trades': history_trades,
        'realized_pnl': round(realized_pnl, 2),
        'open_pnl': round(open_pnl, 2),
        'total_pnl': total_pnl,
        'today_pnl': total_pnl,
    })


@app.route('/api/drma_stocks/watchlist', methods=['GET', 'POST'])
def api_drma_stocks_watchlist():
    """Get or update watchlist tickers for DRMA Stocks."""
    from strategies.drma_stocks.watchlist import add_ticker, load_watchlist, remove_ticker, toggle_ticker

    if request.method == 'GET':
        wl = load_watchlist(ROOT)
        return jsonify({'status': 'ok', 'watchlist': wl, 'tickers': wl})

    data = request.get_json(force=True) or {}
    action = data.get('action', 'add').lower()
    symbol = data.get('symbol', '').strip().upper()
    name = data.get('name', '')

    if action == 'add':
        ok, msg = add_ticker(symbol, name=name, root=ROOT)
    elif action == 'remove':
        ok, msg = remove_ticker(symbol, root=ROOT)
    elif action == 'toggle':
        enabled = data.get('enabled')
        ok, msg = toggle_ticker(symbol, enabled=enabled, root=ROOT)
    else:
        return jsonify({'status': 'error', 'message': f'Unknown action {action}'}), 400

    return jsonify({
        'status': 'ok' if ok else 'error',
        'message': msg,
        'watchlist': load_watchlist(ROOT),
    }), 200 if ok else 400


@app.route('/api/drma_stocks/close_trade', methods=['POST'])
def api_drma_stocks_close_trade():
    """Manually close an active stock equity or options spread trade."""
    import glob
    data = request.get_json(force=True) or {}
    trade_id = data.get('trade_id')
    if not trade_id:
        return jsonify({'status': 'error', 'message': 'Missing trade_id'}), 400

    active_dir = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_STOCKS, ROOT)
    target_path = os.path.join(active_dir, f"{trade_id}.json")
    if not os.path.isfile(target_path):
        for f in glob.glob(os.path.join(active_dir, f"*{trade_id}*.json")):
            target_path = f
            break

    if not os.path.isfile(target_path):
        return jsonify({'status': 'error', 'message': f'Trade {trade_id} not found'}), 404

    try:
        from strategies.drma_stocks.settlement import move_trade_to_history, settle_stock_trade
        with open(target_path, 'r', encoding='utf-8') as f:
            t = json.load(f)
        current_mark = float(t.get('current_mark') or t.get('entry_price') or 0.0)
        settle_stock_trade(t, current_mark, exit_reason='Manual User Close')
        with open(target_path, 'w', encoding='utf-8') as f:
            json.dump(t, f, indent=2)
        move_trade_to_history(target_path, hist_dir)
        return jsonify({'status': 'ok', 'message': f'Closed trade {trade_id}'})
    except Exception as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 500


@app.route('/api/drma_stocks/history')
def api_drma_stocks_history():
    """Get aggregated performance metrics for DRMA Stocks per ticker and per sub-strategy."""
    from dashboard.db import get_stocks_performance_breakdown
    return jsonify(get_stocks_performance_breakdown(include_paper=True))


@app.route('/api/iron_fly/today')
def api_iron_fly_today():
    """Iron Fly status for today's trade."""
    import glob
    from common.session_cleanup import central_today

    paper = _strategy_paper_flag(trades_layout.STRATEGY_IRON_FLY)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')
    book_path = os.path.join(active, f'iron_fly_{today}.json')
    history_book_path = os.path.join(hist_dir, f'iron_fly_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    book_data = None
    for candidate_path in (book_path, history_book_path):
        if not os.path.isfile(candidate_path):
            continue
        try:
            with open(candidate_path, encoding='utf-8') as f:
                book_data = json.load(f)
            break
        except (OSError, json.JSONDecodeError):
            book_data = None

    history = []
    if os.path.isdir(hist_dir):
        for path in sorted(glob.glob(os.path.join(hist_dir, f'iron_fly_{today}.json')), reverse=True):
            try:
                with open(path, encoding='utf-8') as f:
                    row = json.load(f)
                history.append(row)
            except (OSError, json.JSONDecodeError):
                continue

    return jsonify({
        'strategy': trades_layout.STRATEGY_IRON_FLY,
        'paper': paper,
        'date': today,
        'session': session,
        'book': book_data,
        'history': history,
    })


@app.route('/api/iron_fly/approve', methods=['POST'])
def api_iron_fly_approve():
    data = request.get_json(force=True) or {}
    fly_id = data.get('fly_id')
    from strategies.loader import load_strategies
    for strat, cfg in load_strategies():
        if cfg.name == trades_layout.STRATEGY_IRON_FLY and hasattr(strat, 'approve_fly'):
            ok = strat.approve_fly(fly_id)
            return jsonify({'status': 'ok' if ok else 'error', 'fly_id': fly_id})
    return jsonify({'status': 'error', 'message': 'strategy not running or not found'}), 404


@app.route('/api/iron_fly/skip', methods=['POST'])
def api_iron_fly_skip():
    data = request.get_json(force=True) or {}
    fly_id = data.get('fly_id')
    from strategies.loader import load_strategies
    for strat, cfg in load_strategies():
        if cfg.name == trades_layout.STRATEGY_IRON_FLY and hasattr(strat, 'skip_fly'):
            ok = strat.skip_fly(fly_id)
            return jsonify({'status': 'ok' if ok else 'error', 'fly_id': fly_id})
    return jsonify({'status': 'error', 'message': 'strategy not running or not found'}), 404


@app.route('/api/iron_fly/close_all', methods=['POST'])
def api_iron_fly_close_all():
    paper = _strategy_paper_flag(trades_layout.STRATEGY_IRON_FLY)
    from common.manual_close import close_all_structures
    results = close_all_structures(trades_layout.STRATEGY_IRON_FLY, root=ROOT, paper=paper)
    return jsonify({'status': 'ok', 'results': [r.__dict__ for r in results]})


@app.route('/api/iron_fly_xsp/today')
def api_iron_fly_xsp_today():
    """XSP Iron Fly status for today's trade."""
    import glob
    from common.session_cleanup import central_today

    paper = _strategy_paper_flag(trades_layout.STRATEGY_IRON_FLY_XSP)
    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_IRON_FLY_XSP, ROOT)
    session_path = os.path.join(active, f'session_{today}.json')
    book_path = os.path.join(active, f'iron_fly_xsp_{today}.json')
    history_book_path = os.path.join(hist_dir, f'iron_fly_xsp_{today}.json')

    session = {}
    if os.path.isfile(session_path):
        try:
            with open(session_path, encoding='utf-8') as f:
                session = json.load(f)
        except (OSError, json.JSONDecodeError):
            pass

    book_data = None
    for candidate_path in (book_path, history_book_path):
        if not os.path.isfile(candidate_path):
            continue
        try:
            with open(candidate_path, encoding='utf-8') as f:
                book_data = json.load(f)
            break
        except (OSError, json.JSONDecodeError):
            book_data = None

    history = []
    if os.path.isdir(hist_dir):
        for path in sorted(glob.glob(os.path.join(hist_dir, f'iron_fly_xsp_{today}.json')), reverse=True):
            try:
                with open(path, encoding='utf-8') as f:
                    row = json.load(f)
                history.append(row)
            except (OSError, json.JSONDecodeError):
                continue

    return jsonify({
        'strategy': trades_layout.STRATEGY_IRON_FLY_XSP,
        'paper': paper,
        'date': today,
        'session': session,
        'book': book_data,
        'history': history,
    })


@app.route('/api/iron_fly_xsp/approve', methods=['POST'])
def api_iron_fly_xsp_approve():
    data = request.get_json(force=True) or {}
    pair_id = data.get('pair_id') or data.get('fly_id')
    from strategies.loader import load_strategies
    for strat, cfg in load_strategies():
        if cfg.name == trades_layout.STRATEGY_IRON_FLY_XSP and hasattr(strat, 'approve_fly'):
            ok = strat.approve_fly(pair_id)
            return jsonify({'status': 'ok' if ok else 'error', 'pair_id': pair_id})
    return jsonify({'status': 'error', 'message': 'strategy not running or not found'}), 404


@app.route('/api/iron_fly_xsp/skip', methods=['POST'])
def api_iron_fly_xsp_skip():
    data = request.get_json(force=True) or {}
    pair_id = data.get('pair_id') or data.get('fly_id')
    from strategies.loader import load_strategies
    for strat, cfg in load_strategies():
        if cfg.name == trades_layout.STRATEGY_IRON_FLY_XSP and hasattr(strat, 'skip_fly'):
            ok = strat.skip_fly(pair_id)
            return jsonify({'status': 'ok' if ok else 'error', 'pair_id': pair_id})
    return jsonify({'status': 'error', 'message': 'strategy not running or not found'}), 404


@app.route('/api/iron_fly_xsp/close_all', methods=['POST'])
def api_iron_fly_xsp_close_all():
    paper = _strategy_paper_flag(trades_layout.STRATEGY_IRON_FLY_XSP)
    from common.manual_close import close_all_structures
    results = close_all_structures(trades_layout.STRATEGY_IRON_FLY_XSP, root=ROOT, paper=paper)
    return jsonify({'status': 'ok', 'results': [r.__dict__ for r in results]})


@app.route('/api/strategies/control_status', methods=['GET'])
def api_strategies_control_status():
    return jsonify({
        'status': 'ok',
        'strategies': _get_all_strategy_controls(),
    })


@app.route('/api/strategy/<name>/control', methods=['POST'])
def api_strategy_control(name):
    data = request.get_json(force=True) or {}
    enabled = data.get('enabled') if 'enabled' in data else None
    paper = data.get('paper') if 'paper' in data else None
    if enabled is None and paper is None:
        return jsonify({'error': 'must provide enabled or paper boolean'}), 400
    try:
        updated = _set_strategy_control(name, enabled=enabled, paper=paper)
        return jsonify({'status': 'ok', 'strategy': updated})
    except Exception as exc:
        log.exception('Failed to set strategy control for %s', name)
        return jsonify({'error': str(exc)}), 500


@app.route('/api/system/status', methods=['GET'])
def api_system_status():
    from dashboard.process_manager import get_all_services_status, get_strategies_inspect_status
    return jsonify({
        'status': 'ok',
        'services': get_all_services_status(),
        'strategies': get_strategies_inspect_status(),
    })


@app.route('/api/system/service/start', methods=['POST'])
def api_system_service_start():
    data = request.get_json(force=True) or {}
    svc = data.get('service')
    if not svc:
        return jsonify({'error': 'service is required'}), 400
    from dashboard.process_manager import start_service
    res = start_service(svc)
    return jsonify(res)


@app.route('/api/system/service/restart', methods=['POST'])
def api_system_service_restart():
    data = request.get_json(force=True) or {}
    svc = data.get('service')
    if not svc:
        return jsonify({'error': 'service is required'}), 400
    from dashboard.process_manager import restart_service
    res = restart_service(svc)
    return jsonify(res)


@app.route('/api/system/service/stop', methods=['POST'])
def api_system_service_stop():
    data = request.get_json(force=True) or {}
    svc = data.get('service')
    if not svc:
        return jsonify({'error': 'service is required'}), 400
    from dashboard.process_manager import stop_service
    res = stop_service(svc)
    return jsonify(res)


@app.route('/api/system/strategy/rearm', methods=['POST'])
def api_system_strategy_rearm():
    data = request.get_json(force=True) or {}
    strat = data.get('strategy')
    if not strat:
        return jsonify({'error': 'strategy is required'}), 400
    from dashboard.process_manager import rearm_strategy_state
    res = rearm_strategy_state(strat)
    return jsonify(res)


@app.route('/api/system/strategy/rearm_all', methods=['POST'])
def api_system_strategy_rearm_all():
    from dashboard.process_manager import rearm_all_strategies
    res = rearm_all_strategies()
    return jsonify(res)


@app.route('/api/strategy/<name>/config')
def api_strategy_config(name):
    _, cfg, cfg_cls = _resolve_strategy_config(name)
    if cfg is None:
        if name == 'CLONE_TRADES':
            st = _get_strategy_control_status('CLONE_TRADES')
            return jsonify({
                'strategy': name,
                'yaml_defaults': {'paper': False, 'enabled': True},
                'overrides': {'paper': st['paper'], 'enabled': st['enabled']},
                'effective': {'paper': st['paper'], 'enabled': st['enabled']},
            })
        return jsonify({'error': f'unknown strategy {name!r}'}), 404
    from common.daily_overrides import effective_config, get_overrides
    eff = effective_config(cfg, name, ROOT)
    return jsonify({
        'strategy': name,
        'yaml_defaults': cfg.to_dict(),
        'overrides': get_overrides(name, ROOT),
        'effective': eff.to_dict(),
    })


@app.route('/api/strategy/<name>/overrides', methods=['POST'])
def api_strategy_overrides_post(name):
    data = request.get_json(force=True) or {}
    if not isinstance(data, dict):
        return jsonify({'error': 'body must be a JSON object'}), 400

    # If it's a control field (enabled/paper/sub-strategy paper), also trigger _set_strategy_control or persist
    if any(k in data for k in ('enabled', 'paper', 'dir_paper', 'non_dir_paper', 'futures_paper')):
        try:
            if 'enabled' in data or 'paper' in data:
                _set_strategy_control(
                    name,
                    enabled=data.get('enabled'),
                    paper=data.get('paper'),
                )
            from common.strategy_modes import set_strategy_paper_mode
            for sub_k in ('dir_paper', 'non_dir_paper', 'futures_paper'):
                if sub_k in data:
                    set_strategy_paper_mode(name, paper=bool(data[sub_k]), root=ROOT, sub_key=sub_k)
        except Exception as exc:
            log.warning('Could not sync control state for %s: %s', name, exc)

    _, cfg, cfg_cls = _resolve_strategy_config(name)
    if cfg is None or cfg_cls is None:
        if name in ('CLONE_TRADES', 'MEIC_IC'):
            return jsonify({'status': 'ok', 'overrides': data})
        return jsonify({'error': f'unknown strategy {name!r}'}), 404
    try:
        from common.daily_overrides import set_overrides
        overrides = set_overrides(name, ROOT, data, cfg_cls)
        if any(k in data for k in ('dir_paper', 'non_dir_paper', 'futures_paper')):
            emit_summary_update()
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    return jsonify({'status': 'ok', 'overrides': overrides})


@app.route('/api/strategy/<name>/overrides/<key>', methods=['DELETE'])
def api_strategy_overrides_delete(name, key):
    _, cfg, _ = _resolve_strategy_config(name)
    if cfg is None:
        return jsonify({'error': f'unknown strategy {name!r}'}), 404
    from common.daily_overrides import delete_override
    overrides = delete_override(name, ROOT, key)
    try:
        from common.strategy_modes import delete_strategy_paper_mode
        delete_strategy_paper_mode(name, key=key, root=ROOT)
    except Exception:
        pass
    return jsonify({'status': 'ok', 'overrides': overrides})


@app.route('/api/strategy/<name>/close', methods=['POST'])
def api_strategy_close(name):
    if name == 'PERMA_BULL_PCS':
        return api_perma_bull_close()
    data = request.get_json(force=True) or {}
    structure = data.get('structure', 'all')
    trade_id = data.get('trade_id')
    paper = _strategy_paper_flag(name)
    from common.manual_close import close_all_structures, close_structure
    if structure == 'all' and not trade_id:
        results = close_all_structures(name, root=ROOT, paper=paper)
        return jsonify({
            'status': 'ok',
            'results': [r.__dict__ for r in results],
        })
    result = close_structure(name, structure, root=ROOT, paper=paper, trade_id=trade_id)
    code = 200 if result.success else 400
    return jsonify({'status': 'ok' if result.success else 'error', **result.__dict__}), code


def _persist_env_updates(updates: dict[str, str]) -> None:
    env_file = os.path.join(ROOT, '.env')
    if not os.path.isfile(env_file):
        lines = []
    else:
        with open(env_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    existing_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith('#') and '=' in stripped:
            key = stripped.split('=', 1)[0].strip()
            if key in updates:
                new_lines.append(f'{key}={updates[key]}\n')
                existing_keys.add(key)
                continue
        if not line.endswith('\n'):
            line += '\n'
        new_lines.append(line)

    for k, v in updates.items():
        if k not in existing_keys:
            new_lines.append(f'{k}={v}\n')

    with open(env_file, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)


@app.route('/api/system_config', methods=['GET'])
def api_get_system_config():
    from common.trading_gate import read_state, gate_enabled
    from common.market_hours import is_early_close_day_ct, market_close_time_ct
    from meic0dte.app.utilities import central_date

    today = central_date()
    early_close = is_early_close_day_ct(today)
    close_time = market_close_time_ct(today).strftime('%H:%M')
    gate_state = read_state()

    low_mem = os.environ.get('LOW_MEMORY_MODE', 'false').lower() in ('1', 'true', 'yes')
    sidecar_opt = os.environ.get('MEIC_SIDE_OPTION_COLLECTION', 'true').lower() in ('1', 'true', 'yes')
    if low_mem:
        sidecar_opt = False

    ladder_symbols = int(os.environ.get('SPX_LADDER_MAX_ACTIVE_SYMBOLS', '50' if low_mem else '500'))
    in_process = os.environ.get('IN_PROCESS_SERVICES', 'false').lower() in ('1', 'true', 'yes') or low_mem
    sms_enabled = os.environ.get('SMS_NOTIFICATIONS_ENABLED', 'true').lower() in ('1', 'true', 'yes')
    sms_to = os.environ.get('SMS_TO_NUMBER', os.environ.get('SMS_PHONE_NUMBER', '')).strip()
    sunday_futures = os.environ.get('SUNDAY_FUTURES_ENABLED', 'true').lower() in ('1', 'true', 'yes')
    log_retention = int(os.environ.get('LOG_RETENTION_DAYS', '14'))

    return jsonify({
        'status': 'ok',
        'config': {
            'new_risk_gate_enabled': gate_enabled(),
            'trading_gate': {
                'latched': bool(gate_state.get('new_risk_latched')),
                'latch_reason': gate_state.get('latch_reason') or '',
                'rest_status': gate_state.get('rest_status') or 'unknown',
                'cooldown_active': bool(gate_state.get('cooldown_active')),
                'session_date_ct': gate_state.get('session_date_ct') or '',
            },
            'low_memory_mode': low_mem,
            'meic_side_option_collection': sidecar_opt,
            'spx_ladder_max_symbols': ladder_symbols,
            'in_process_services': in_process,
            'sms_notifications_enabled': sms_enabled,
            'sms_to_number': sms_to,
            'sunday_futures_launch_enabled': sunday_futures,
            'log_retention_days': log_retention,
            'nyse_schedule': {
                'today': today.isoformat(),
                'is_early_close': early_close,
                'close_time_ct': close_time,
                'status_label': f'Early Close ({close_time} CT)' if early_close else f'Normal Close ({close_time} CT)',
            },
        },
    })


@app.route('/api/system_config', methods=['POST'])
def api_update_system_config():
    data = request.get_json(force=True) or {}
    env_updates = {}

    if 'new_risk_gate_enabled' in data:
        val = 'true' if data['new_risk_gate_enabled'] else 'false'
        os.environ['NEW_RISK_GATE_ENABLED'] = val
        env_updates['NEW_RISK_GATE_ENABLED'] = val

    if 'low_memory_mode' in data:
        val = 'true' if data['low_memory_mode'] else 'false'
        os.environ['LOW_MEMORY_MODE'] = val
        env_updates['LOW_MEMORY_MODE'] = val
        if data['low_memory_mode']:
            os.environ['SPX_LADDER_MAX_ACTIVE_SYMBOLS'] = '50'
            os.environ['MEIC_SIDE_OPTION_COLLECTION'] = 'false'
            env_updates['SPX_LADDER_MAX_ACTIVE_SYMBOLS'] = '50'
            env_updates['MEIC_SIDE_OPTION_COLLECTION'] = 'false'

    if 'meic_side_option_collection' in data and not data.get('low_memory_mode'):
        val = 'true' if data['meic_side_option_collection'] else 'false'
        os.environ['MEIC_SIDE_OPTION_COLLECTION'] = val
        env_updates['MEIC_SIDE_OPTION_COLLECTION'] = val

    if 'spx_ladder_max_symbols' in data:
        try:
            val = str(max(10, min(1000, int(data['spx_ladder_max_symbols']))))
            os.environ['SPX_LADDER_MAX_ACTIVE_SYMBOLS'] = val
            env_updates['SPX_LADDER_MAX_ACTIVE_SYMBOLS'] = val
        except (ValueError, TypeError):
            pass

    if 'sms_notifications_enabled' in data:
        val = 'true' if data['sms_notifications_enabled'] else 'false'
        os.environ['SMS_NOTIFICATIONS_ENABLED'] = val
        env_updates['SMS_NOTIFICATIONS_ENABLED'] = val

    if 'sms_to_number' in data:
        phone = str(data['sms_to_number']).strip()
        os.environ['SMS_TO_NUMBER'] = phone
        os.environ['SMS_PHONE_NUMBER'] = phone
        env_updates['SMS_TO_NUMBER'] = phone

    if 'sunday_futures_launch_enabled' in data:
        val = 'true' if data['sunday_futures_launch_enabled'] else 'false'
        os.environ['SUNDAY_FUTURES_ENABLED'] = val
        env_updates['SUNDAY_FUTURES_ENABLED'] = val

    if 'log_retention_days' in data:
        try:
            days = str(max(1, min(90, int(data['log_retention_days']))))
            os.environ['LOG_RETENTION_DAYS'] = days
            env_updates['LOG_RETENTION_DAYS'] = days
        except (ValueError, TypeError):
            pass

    try:
        _persist_env_updates(env_updates)
    except Exception as exc:
        log.warning('Could not persist updates to .env: %s', exc)

    emit_summary_update()
    return api_get_system_config()


@app.route('/api/system_config/test_sms', methods=['POST'])
def api_test_sms():
    from common.notifications import dispatch_notification

    to_num = os.environ.get('SMS_TO_NUMBER', os.environ.get('SMS_PHONE_NUMBER', '')).strip()
    msg = f"[MEIC Test] SMS notification test from dashboard at {_central_now_str()}"
    ok, detail = dispatch_notification(msg, force_sms=True)
    if ok:
        return jsonify({'status': 'ok', 'message': f'Test SMS dispatched to {to_num or "configured number"}'})
    return jsonify({'status': 'error', 'error': detail or 'SMS dispatch failed'}), 400


@app.route('/api/system_config/resume_gate', methods=['POST'])
def api_system_resume_gate():
    from common.trading_gate import resume_new_risk, _mutate
    from common.broker_cooldown import clear_cooldown

    clear_cooldown()

    def _clear(s):
        s['new_risk_latched'] = False
        s['latch_reason'] = ''
        s['latch_source'] = ''

    _mutate(_clear)
    resume_new_risk(cleared_by='operator')
    emit_summary_update()
    return jsonify({'status': 'ok', 'message': 'Trading gate resumed and latches cleared'})

# ── Entry point ───────────────────────────────────────────────────────────────
register_manual_spread_routes(
    app,
    live_prices=live_prices,
    index_topic=INDEX_TOPIC,
    trades_dir=TRADES_DIR,
    central_now_str=_central_now_str,
    tranche_lots=TRANCHE_LOTS,
    tranche_sides=TRANCHE_SIDES,
    notify_update=emit_summary_update,
    launcher_active_fn=_launcher_active,
)
register_gex_routes(app)
try:
    from clone_trades.handlers import register_clone_trades_routes
    register_clone_trades_routes(app)
except Exception as _ct_exc:
    log.warning('Failed to register clone trades routes: %s', _ct_exc)

if __name__ == '__main__':
    import logging

    from common.process_lock import process_lock
    from common.logging_config import silence_noisy_loggers

    silence_noisy_loggers('werkzeug')
    bind_host = os.environ.get('DASHBOARD_BIND_HOST', '0.0.0.0').strip() or '0.0.0.0'
    with process_lock('dashboard', command='dashboard/server.py'):
        socketio.run(app, host=bind_host, port=5002, debug=False, allow_unsafe_werkzeug=True)
