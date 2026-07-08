"""
MEIC Dashboard Server (TastyTrade)
Run with:  python dashboard/server.py
Then open:  http://localhost:5002
"""
import os, sys, json, subprocess, threading, time, signal, glob, logging
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

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from typing import Optional

from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

from streaming.config import MQTT_BROKER_ADDR

log = logging.getLogger(__name__)
from common import tt_config
from common.broker_factory import get_mqtt_topic_prefix
from common.option_prices import sanitize_option_mid
sys.path.insert(0, os.path.dirname(__file__))
from db import (
    upsert_trade, get_trades, get_daily_summary, get_stats, get_stats_by_strategy,
    get_daily_breakdown, delete_trade, get_conn, STRATEGY_MEIC, STRATEGY_MANUAL,
)
from manual_spread_handlers import (
    build_manual_trades,
    commands_dir_for_filename,
    register_manual_spread_routes,
)
from gex_handlers import register_gex_routes
from common.trade_pick import pick_best_trade
from blocks.stop.close_fills import display_close_prices, slippage_dollars, slippage_label
from blocks.stop.breach_watch import breach_display_fields
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

TRANCHE_LOTS = ['11-00', '12-00', '12-30', '01-15', '01-45', '02-00']
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

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), 'templates'))
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


def tail_log(path, n=40):
    try:
        with open(path, errors='replace') as f:
            lines = f.readlines()
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


def _phase_display(phases: dict, stop_multiplier: Optional[float] = None) -> str:
    if phases.get('phase3_activated_at'):
        label = PHASE_DISPLAY[3]
    elif phases.get('phase2_activated_at'):
        label = PHASE_DISPLAY[2]
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
) -> str:
    if status == 'closed':
        if close_mechanism in KILL_MECHANISMS:
            if strategy == STRATEGY_MANUAL:
                return 'closed'
            return 'killed'
        if close_mechanism in BREACH_MECHANISMS:
            return 'breached'
        return 'closed'
    if status == 'closing':
        return 'closing'
    if status in ('open', 'pending_fill'):
        return 'open'
    return status


def _stop_price_label(active_stop: dict) -> str:
    """Format active stop as stop/limit prices."""
    if not active_stop:
        return ''
    sp = active_stop.get('stop_price')
    lp = active_stop.get('limit_price')
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
    """
    sc = float(short_close) if short_close is not None else None
    lc = float(long_close) if long_close is not None else None

    if status == 'closed' and sc is not None and lc is not None:
        exit_credit = round(sc - lc, 2)
        pnl = round((net_credit - exit_credit) * 100 * quantity, 2)
        return pnl, exit_credit, sc, True

    if trade is not None and spx_close is not None:
        from common.expiry_settlement import compute_settled_pnl
        settled = compute_settled_pnl(trade, spx_close)
        if settled is not None and settled.get('settled'):
            exit_credit = settled['close_debit']
            return settled['pnl'], exit_credit, settled['short_close_price'], True

    if sc is not None:
        long_for_exit = lc if lc is not None else cur_long
        exit_credit = round(sc - long_for_exit, 2)
        pnl = round((net_credit - exit_credit) * 100 * quantity, 2)
        return pnl, exit_credit, sc, True

    cur_spread = round(cur_short - cur_long, 2)
    pnl = round((net_credit - cur_spread) * 100 * quantity, 2)
    return pnl, None, None, False


def _read_active_trades():
    """Read all per-trade JSON from trades/active/{strategy}/."""
    trades = []
    try:
        from blocks.stop import state as state_mod
        from blocks.stop.pending_fill_sync import sync_pending_fills
        from common.broker_factory import get_shared_broker
        from dashboard.broker_fill_sync import maybe_sync_active_trades

        maybe_sync_active_trades(
            read_json=read_json_safe,
            iter_paths=state_mod.iter_active_trade_paths,
            get_broker_fn=get_shared_broker,
            sync_fn=sync_pending_fills,
        )

        for fpath in state_mod.iter_active_trade_paths():
            state = read_json_safe(fpath)
            if state and isinstance(state, dict):
                state['_filepath'] = fpath
                state['_filename'] = os.path.basename(fpath)
                trades.append(state)
    except Exception:
        pass
    return trades



def _match_trade_to_slot(trade, lot, side):
    """Check if a trade JSON belongs to a given lot+side slot."""
    entry = trade.get('entry', {})
    trade_lot = trade.get('lot', '') or entry.get('lot', '')
    trade_side = entry.get('side', '')
    return trade_lot == lot and trade_side == side


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


def _resolve_trade_for_row(row, active_trades):
    if row.trade_path:
        resolved = _resolve_trade_path(row.trade_path)
        if resolved and os.path.isfile(resolved):
            state = read_json_safe(resolved)
            if state:
                state['_filepath'] = resolved
                state['_filename'] = os.path.basename(resolved)
                return state
    matching = [t for t in active_trades if _match_trade_to_slot(t, row.lot, row.side)]
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
            )
    if row.state == 'entering':
        return 'entering'
    if row.state == 'failed':
        return 'failed'
    if trade:
        entry = trade.get('entry') or {}
        return _slot_state_from_trade(
            trade.get('status', 'unknown'),
            trade.get('close_mechanism'),
            strategy=entry.get('strategy'),
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
    live_pnl, exit_credit, exit_short, pnl_frozen = _trade_pnl(
        net_credit, quantity, short_fill, long_fill,
        short_close, long_close, cur_short, cur_long, status,
        trade=trade, spx_close=spx_settle,
    )

    phase_label = _phase_display(phases, stop_multiplier_for_state(trade))
    active_stop = _resolve_active_stop(trade)
    stop_order_id = active_stop.get('order_id') or ''

    entry_label = _leg_fill_label(net_credit, short_fill, long_fill)
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
        'stop_order_id': stop_order_id,
        'stop_price': active_stop.get('stop_price'),
        'limit_price': active_stop.get('limit_price'),
        'stop_label': _stop_price_label(active_stop),
        'close_mechanism': close_mechanism or '',
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


def build_summary():
    active_trades = _read_active_trades()
    bot_status = read_bot_status()
    session_plan = _meic_session_rows()

    from common.expiry_settlement import ensure_spx_settlement_close, get_spx_settlement_close, settlement_cutoff_reached
    from common.session_cleanup import central_today

    today = central_today()
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
        trade = _resolve_trade_for_row(row, active_trades) if session_plan else pick_best_trade(
            [t for t in active_trades if _match_trade_to_slot(t, lot, side)]
        )

        slot = {
            'lot': lot,
            'side': side,
            'slot_key': slot_key,
            'session_state': getattr(row, 'state', 'pending'),
            'stop_multiplier': getattr(row, 'stop_multiplier', 2),
            'paused': getattr(row, 'paused', False),
        }
        if session_plan:
            slot.update({
                'skip': getattr(row, 'skip', False),
                'plan_quantity': getattr(row, 'quantity', 1),
                'width': getattr(row, 'width', '25-35'),
                'credit_min': getattr(row, 'credit_min', 0.9),
                'credit_max': getattr(row, 'credit_max', 1.85),
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
            )
        else:
            slot['state'] = 'pending'

        if trade:
            status, live_pnl, entry, short_sym, long_sym, short_leg, long_leg, quantity, net_credit, trade = (
                _apply_trade_overlay(slot, trade, lot, side, spx_settle=spx_settle)
            )
            if session_plan:
                slot['state'] = _session_display_state(row, trade)

            if status == 'closed':
                slip_usd = slippage_dollars(trade)
                if slip_usd is not None:
                    day_slippage += slip_usd

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
        'combined_pnl':  round(day_pnl + manual_pnl, 2),
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
    paths = {
        'launcher': _launcher_log_path(),
        'stream': _stream_log_path(),
        'stop_monitor': STOP_MONITOR_LOG,
    }
    path = paths.get(name)
    if not path:
        return jsonify({'error': 'unknown log'}), 404
    if not os.path.isfile(path):
        return jsonify({'log': '(log not available)'})
    return jsonify({'log': tail_log(path)})

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
    from dashboard.broker_fill_sync import fill_sync_stats

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
        'shared_broker': shared_broker_stats(),
        'locks': locks,
        'launcher_active': _launcher_active(),
        'dashboard_pid': os.getpid(),
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

    allowed = {
        'paused', 'skip', 'quantity', 'stop_multiplier', 'stop_percent',
        'width', 'credit_min', 'credit_max', 'chase1_mode', 'chase1_max',
        'chase2_mode', 'chase2_max', 'fill_wait_sec', 'max_attempts',
        'entry_window_start', 'entry_window_end',
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return jsonify({'error': 'no valid fields'}), 400

    from blocks.session.plan import parse_time_field, format_time_field

    for time_key in ('entry_window_start', 'entry_window_end'):
        if time_key in updates:
            try:
                updates[time_key] = format_time_field(parse_time_field(str(updates[time_key])))
            except ValueError as exc:
                return jsonify({'error': str(exc)}), 400

    if row.state not in ('pending', 'entering') and any(k != 'paused' for k in updates):
        return jsonify({'error': f'row state {row.state} is not editable'}), 400

    plan.update_row(slot_key, **updates)
    plan.save()
    return jsonify({'status': 'ok', 'row': plan.row_by_slot_key(slot_key).to_dict()})


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
    return jsonify({'status': 'ok', 'updated': updated, 'skipped': skipped})


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

    plan.update_row(slot_key, stop_multiplier=float(stop_multiplier))
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
    with open(cmd_path, 'w', encoding='utf-8') as f:
        json.dump({
            'stop_multiplier': float(stop_multiplier),
            'stop_mode': data.get('stop_mode', 'multiplier'),
            'ts': _central_now_str(),
            'source': 'dashboard',
        }, f)

    return jsonify({
        'status': 'ok',
        'row': row.to_dict(),
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

@app.route('/api/history')
def api_history():
    from dashboard.history_sync import sync_history_from_disk

    sync_history_from_disk(ROOT)
    date  = request.args.get('date')
    strategy = request.args.get('strategy')
    limit = int(request.args.get('limit', 200))
    return jsonify(get_trades(date=date, strategy=strategy, limit=limit))


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


@app.route('/api/daily_summary')
def api_daily_summary():
    from dashboard.history_sync import sync_history_from_disk

    sync_history_from_disk(ROOT)
    days = int(request.args.get('days', 30))
    strategy = request.args.get('strategy')
    return jsonify(get_daily_summary(days=days, strategy=strategy))

@app.route('/api/stats')
def api_stats():
    from dashboard.history_sync import sync_history_from_disk

    sync_history_from_disk(ROOT)
    strategy = request.args.get('strategy')
    if strategy:
        return jsonify(get_stats(strategy=strategy))
    payload = get_stats_by_strategy()
    payload['total_pnl'] = payload['all']['total_pnl']
    payload['total_trades'] = payload['all']['total_trades']
    payload['win_rate'] = payload['all']['win_rate']
    return jsonify(payload)

@app.route('/api/daily_calendar')
def api_daily_calendar():
    from dashboard.history_sync import sync_history_from_disk

    sync_history_from_disk(ROOT)
    days = int(request.args.get('days', 31))
    strategy = request.args.get('strategy')
    rows = get_daily_breakdown(days=days)
    if strategy:
        key = 'meic_pnl' if strategy == STRATEGY_MEIC else 'manual_pnl'
        trade_key = 'meic_trades' if strategy == STRATEGY_MEIC else 'manual_trades'
        win_key = 'meic_wins' if strategy == STRATEGY_MEIC else 'manual_wins'
        filtered = []
        for row in rows:
            n = row.get(trade_key, 0)
            filtered.append({
                'date': row['date'],
                'pnl': row.get(key, 0),
                'winRate': round((row.get(win_key, 0) / n * 100) if n else 0, 0),
                'numTrades': n,
                'meic_trades': row.get('meic_trades', 0),
                'manual_trades': row.get('manual_trades', 0),
                'meic_pnl': row.get('meic_pnl', 0),
                'manual_pnl': row.get('manual_pnl', 0),
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
        })
    return jsonify(result)

@app.route('/api/trade/<int:trade_id>', methods=['DELETE'])
def api_delete_trade(trade_id):
    delete_trade(trade_id)
    return jsonify({'status': 'deleted', 'id': trade_id})

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

if __name__ == '__main__':
    import logging

    from common.process_lock import process_lock
    from common.logging_config import silence_noisy_loggers

    silence_noisy_loggers('werkzeug')
    with process_lock('dashboard', command='dashboard/server.py'):
        socketio.run(app, host='0.0.0.0', port=5002, debug=False, allow_unsafe_werkzeug=True)
