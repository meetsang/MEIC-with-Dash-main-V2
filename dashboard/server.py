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
    return is_strategy_paper(name, default=True)

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
        if cm in ('profit_target', 'profit_target_hit'):
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
    if status in ('open', 'pending_fill'):
        return 'open'
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

    if status == 'closed' and sc is not None and lc is not None:
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
        exit_credit = round(sc - long_for_exit, 2)
        pnl = round((net_credit - exit_credit) * 100 * active_qty + scaled_pnl, 2)
        return pnl, exit_credit, sc, True

    cur_s = cur_short if cur_short is not None else (short_fill or 0.0)
    cur_l = cur_long if cur_long is not None else (long_fill or 0.0)
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


def _resolve_trade_path(trade_path: str) -> str:
    """Resolve session CSV trade_path — active file or archived copy under history/."""
    if not trade_path:
        return ''
    if os.path.isfile(trade_path):
        return trade_path
    base = os.path.basename(trade_path)
    from common import trades_layout
    for rel in (trades_layout.MEIC_HISTORY, trades_layout.MANUAL_HISTORY):
        hist_root = os.path.join(ROOT, rel)
        if not os.path.isdir(hist_root):
            continue
        direct = os.path.join(hist_root, base)
        if os.path.isfile(direct):
            return direct
        for day_dir in sorted(glob.glob(os.path.join(hist_root, '*')), reverse=True):
            candidate = os.path.join(day_dir, base)
            if os.path.isfile(candidate):
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
    quantity = int(trade.get('filled_quantity', 1) or 1)
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

    live_pnl, exit_credit, exit_short, pnl_frozen = _trade_pnl(
        net_credit, quantity, short_fill, long_fill,
        short_close, long_close, cur_short, cur_long, status,
        trade=trade, spx_close=trade_spx,
    )

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
        'pnl_class': 'text-success' if live_pnl >= 0 else 'text-danger',
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
        pnl = _handshake_trade_live_pnl(trade)
        rows.append({
            'date': str(entry.get('timestamp') or '')[:10],
            'status': trade.get('status'),
            'short_strike': short_leg.get('strike'),
            'long_strike': long_leg.get('strike'),
            'net_credit': entry.get('net_credit'),
            'realized_pnl_dollars': pnl,
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


def _drma_hm_today_pnl() -> float:
    """Today's DRMA_HM paper/live P/L (realized + open)."""
    import glob
    from common.session_cleanup import central_today
    from strategies.drma_hm.settlement import attach_dollar_pnl_display

    today = central_today().isoformat()
    active = trades_layout.active_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)
    hist_dir = trades_layout.history_dir_for_strategy(trades_layout.STRATEGY_DRMA_HM, ROOT)

    total = 0.0
    for d in (active, hist_dir):
        if not os.path.isdir(d):
            continue
        for path in glob.glob(os.path.join(d, f'drma_hm_{today}_*.json')):
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


def _resolve_strategy_config(name: str):
    lookup_name = 'DRMA_HM_SPX' if name in ('DRMA_HM_DIR', 'DRMA_HM_NONDIR', 'DRMA_HM_FUT') else name
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
                            })
                            _synced_trades.add(_sync_key)
                        except Exception:
                            pass
                else:
                    open_count += 1
                    day_pnl += live_pnl

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

@app.route('/')
def index():
    return render_template('index.html', data=build_summary())

@app.route('/api/summary')
def api_summary():
    return jsonify(build_summary())

@app.route('/api/log/<name>')
def api_log(name):
    from dashboard.process_manager import get_service_status, SERVICES
    paths = {
        'launcher': _launcher_log_path(),
        'stream': _stream_log_path(),
        'streamer': _stream_log_path(),
        'stop_monitor': STOP_MONITOR_LOG,
    }
    path = paths.get(name)
    if not path and name in SERVICES:
        svc_st = get_service_status(name)
        path = svc_st.get('log_path')
    if not path:
        pattern = os.path.join(ROOT, 'logs', f'*{name}*.log')
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
        wait_for_launcher_sec=2.0,
        **{k: data.get(k) for k in (
            'stop_multiplier', 'on_unfilled', 'fill_wait_sec',
            'chase_floor', 'chase_max_attempts', 'max_attempts',
        ) if k in data},
    )
    return jsonify(result), code


@app.route('/api/session/row/stop', methods=['POST'])
def api_session_row_stop():
    """Q6: update session CSV stop + trade JSON + stop_update command file."""
    from blocks.entry.handoff import apply_stop_snapshot
    from blocks.stop import state as state_mod

    data = request.get_json(force=True) or {}
    strategy = data.get('strategy', trades_layout.STRATEGY_MEIC)
    slot_key = data.get('slot_key')
    stop_multiplier = data.get('stop_multiplier')
    if not slot_key or stop_multiplier is None:
        return jsonify({'error': 'slot_key and stop_multiplier required'}), 400

    plan = _meic_session_rows() if strategy == trades_layout.STRATEGY_MEIC else load_manual_session_today(ROOT)
    if plan is None:
        return jsonify({'error': 'session plan not found'}), 404
    row = plan.row_by_slot_key(slot_key)
    if row is None:
        return jsonify({'error': f'unknown slot_key: {slot_key}'}), 404

    updates: Dict[str, Any] = {'stop_multiplier': float(stop_multiplier)}
    if 'profit_target_pct' in data and data['profit_target_pct'] is not None:
        pt_val = float(data['profit_target_pct'])
        if pt_val > 1.0:
            pt_val = pt_val / 100.0
        updates['profit_target_pct'] = pt_val

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

    pt_val = float(profit_target_pct)
    if pt_val > 1.0:
        pt_val = pt_val / 100.0

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

    return jsonify({
        'strategy': trades_layout.STRATEGY_PERMA_BULL,
        'paper': paper,
        'date': today,
        'session': session,
        'trade': trade,
        'history': [],
    })


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

    # Find active trade
    # Find all active trades
    trades = []
    trade = None
    live_es_price = fetch_es_current_price(levels=session.get('levels') or {}, spx_spot=(strat_status.get('close') if strat_status else None))

    if os.path.isdir(active):
        candidates = sorted(glob.glob(os.path.join(active, f'drma_hm_{today}_*.json')), reverse=True)
        all_symbols = []
        open_trades_with_paths = []
        for path in candidates:
            try:
                with open(path, encoding='utf-8') as f:
                    t = json.load(f)
                trades.append(t)
                if t.get('status') == 'open':
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

        trade = trades[0] if trades else None

    history = []
    if os.path.isdir(hist_dir):
        for path in sorted(glob.glob(os.path.join(hist_dir, f'drma_hm_{today}_*.json')), reverse=True):
            try:
                with open(path, encoding='utf-8') as f:
                    row = json.load(f)
                attach_dollar_pnl_display(row)
                if not row.get('date'):
                    entered_at = row.get('entered_at') or ''
                    row['date'] = entered_at[:10] if len(entered_at) >= 10 else today
                history.append(row)
            except (OSError, json.JSONDecodeError):
                continue

    realized_pnl = sum(float(h.get('realized_pnl_dollars') or 0.0) for h in history)
    open_pnl = sum(float(t.get('unrealized_pnl_dollars') or 0.0) for t in trades if t.get('status') == 'open')
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

    # Ensure reference levels are available
    if not levels or levels.get('pdh') is None:
        try:
            from strategies.drma_hm.market_data_feed import fetch_yahoo_market_levels
            from strategies.drma_hm.config import DRMAHMConfig
            cfg = DRMAHMConfig(name=trades_layout.STRATEGY_DRMA_HM, paper=paper)
            raw_levels = fetch_yahoo_market_levels(today_date, cfg, ROOT)
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

    # Ensure indicators are available
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
        'indicators': indicators,
        'levels': levels,
        'trades': trades,
        'trade': trade,
        'history': history,
        'realized_pnl': realized_pnl,
        'open_pnl': open_pnl,
        'total_pnl': total_pnl,
    })


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
    with process_lock('dashboard', command='dashboard/server.py'):
        socketio.run(app, host='0.0.0.0', port=5002, debug=False, allow_unsafe_werkzeug=True)
