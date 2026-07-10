"""Manual Spread dashboard API handlers."""
from __future__ import annotations

import json
import logging
import os

from flask import jsonify, request

from common.broker_factory import get_shared_broker
from manual_spread import config as ms_config
from manual_spread import entry as ms_entry
from blocks.stop import state as state_mod
from blocks.stop.close_fills import slippage_dollars, slippage_label
from blocks.stop.breach_watch import breach_display_fields
from dashboard.runtime_display import (
    breach_readiness_label,
    decorate_entry_label,
    quote_source_label,
)
from dashboard.trade_times import trade_entry_time_iso, trade_exit_time_iso
from blocks.stop.stop_math import stop_multiplier_for_state

log = logging.getLogger(__name__)


def _resolve_active_stop(trade: dict) -> dict:
    """Use working stop from JSON, or latest replaced stop in history."""
    active = dict(trade.get('active_stop') or {})
    st = str(active.get('status', '')).lower()
    if st in ('working', 'live', 'contingent', 'received', 'open', 'partially filled', 'filled'):
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


def _leg_fill_label(credit, short_p, long_p) -> str:
    if short_p is None or long_p is None:
        return f'{credit:.2f}' if credit is not None else ''
    sp = float(short_p)
    lp = float(long_p)
    cr = float(credit) if credit is not None else round(sp - lp, 2)
    return f'{cr:.2f} ({sp:.2f}-{lp:.2f})'


def build_manual_trades(
    *,
    live_price_fn,
    phase_display_fn,
    trade_pnl_fn,
    stop_label_fn,
    slot_state_fn,
    spx_settle=None,
):
    """Return (rows, day_pnl, open_count, day_slippage) for Manual Spread active trades."""
    rows = []
    day_pnl = 0.0
    day_slippage = 0.0
    open_count = 0

    for trade in ms_entry.load_dashboard_manual_trades():
        status = trade.get('status', 'unknown')
        entry = trade.get('entry', {})
        short_leg = trade.get('short_leg', {})
        long_leg = trade.get('long_leg', {})
        phases = trade.get('phases', {})

        short_sym = short_leg.get('symbol', '')
        long_sym = long_leg.get('symbol', '')
        limit_credit = float(entry.get('limit_credit') or entry.get('net_credit') or 0)
        quantity = int(trade.get('quantity') or 1)
        filled = int(trade.get('filled_quantity') or 0)
        short_fill = float(short_leg.get('fill_price') or 0)
        long_fill = float(long_leg.get('fill_price') or 0)
        short_close = trade.get('short_close_price')
        long_close = trade.get('long_close_price')

        from common.option_prices import sanitize_option_mid

        def _leg_mark(sym: str, fill: float) -> float:
            raw = live_price_fn(sym)
            clean = sanitize_option_mid(sym, float(raw) if raw is not None else None)
            return float(clean if clean is not None else fill)

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

        close_mechanism = trade.get('close_mechanism')
        row_state = (
            'working'
            if status == 'pending_fill'
            else slot_state_fn(
                status,
                close_mechanism,
                strategy=entry.get('strategy'),
                trade=trade,
            )
        )

        net = float(entry.get('net_credit') or limit_credit)
        live_pnl = 0.0
        pnl_class = 'text-muted'
        pnl_frozen = False
        exit_credit = None
        exit_short = None
        if status in ('open', 'closing', 'closed') and filled > 0:
            live_pnl, exit_credit, exit_short, pnl_frozen = trade_pnl_fn(
                net, filled,
                short_fill, long_fill,
                short_close, long_close,
                cur_short, cur_long, status,
                trade=trade, spx_close=spx_settle,
            )
            pnl_class = 'text-success' if live_pnl >= 0 else 'text-danger'

        entry_label = ''
        if status == 'pending_fill':
            entry_label = f'lim {limit_credit:.2f} ({filled}/{quantity})'
        elif filled > 0:
            entry_label = decorate_entry_label(
                trade,
                _leg_fill_label(net, short_fill, long_fill),
            )

        exit_label = ''
        if exit_short is not None:
            le = float(long_close) if long_close is not None else float(cur_long)
            asterisk = '*' if long_close is None and status == 'closing' else ''
            exit_label = f'{exit_credit:.2f} ({float(exit_short):.2f}-{le:.2f}{asterisk})'

        slip_usd = None
        if status == 'closed' or short_close is not None:
            slip_usd = slippage_dollars(trade)

        active_stop = _resolve_active_stop(trade)
        stop_order_id = active_stop.get('order_id') or ''
        breach_fields = breach_display_fields(
            trade,
            live_short=live_price_fn(short_sym),
            live_long=live_price_fn(long_sym),
            trade_status=status,
        )
        rows.append({
            'lot': trade.get('lot', ''),
            'side': entry.get('side', ''),
            'short_strike': short_leg.get('strike', ''),
            'long_strike': long_leg.get('strike', ''),
            'limit_credit': limit_credit,
            'filled': filled,
            'quantity': filled if filled else quantity,
            'entry_credit': net if filled else limit_credit,
            'entry_label': entry_label,
            'exit_label': exit_label,
            'live_pnl': live_pnl,
            'pnl_class': pnl_class,
            'pnl_frozen': pnl_frozen,
            'slippage': slip_usd,
            'slippage_label': slippage_label(slip_usd),
            'cur_short': cur_short,
            'cur_long': cur_long,
            'cur_spread': (
                round(exit_credit, 2) if exit_credit is not None and pnl_frozen
                else cur_spread
            ),
            'phase': (
                phase_display_fn(phases, stop_multiplier_for_state(trade))
                if status == 'open'
                else ''
            ),
            'stop_label': stop_label_fn(active_stop),
            'stop_order_id': stop_order_id,
            'state': row_state,
            'status': status,
            'entry_quote_source_label': quote_source_label(trade),
            'breach_readiness_label': breach_readiness_label(trade),
            '_filename': trade.get('_filename', ''),
            'entry_time': trade_entry_time_iso(trade),
            'exit_time': trade_exit_time_iso(trade),
            **breach_fields,
        })

        if status in ('open', 'closing', 'pending_fill'):
            open_count += 1
            if status in ('open', 'closing'):
                day_pnl += live_pnl
        elif status == 'closed':
            day_pnl += live_pnl

        if slip_usd is not None:
            day_slippage += slip_usd

    return rows, round(day_pnl, 2), open_count, round(day_slippage, 2)


def commands_dir_for_filename(filename: str, trades_dir: str) -> str:
    from common import trades_layout
    path = trades_layout.commands_dir()
    os.makedirs(path, exist_ok=True)
    return path


def register_manual_spread_routes(app, *, live_prices, index_topic, trades_dir, central_now_str, tranche_lots, tranche_sides, notify_update=None, launcher_active_fn=None):
    """Register /api/manual_spread/* and /api/pause_all_meic."""
    launcher_active = launcher_active_fn or (lambda: False)

    def _push_update():
        if notify_update:
            try:
                notify_update()
            except Exception:
                pass

    @app.route('/api/pause_all_meic', methods=['POST'])
    def pause_all_meic():
        slots = [f'{lot}_{side}' for lot in tranche_lots for side in tranche_sides]
        root = os.path.abspath(os.path.join(trades_dir, '..'))
        try:
            from blocks.session.bootstrap import bootstrap_meic_session_if_missing
            from blocks.session.plan import load_meic_session_today

            bootstrap_meic_session_if_missing(root)
            plan = load_meic_session_today(root)
            if plan:
                for row in plan.rows:
                    plan.update_row(row.slot_key, paused=True)
                plan.save()
        except Exception:
            pass

        return jsonify({'status': 'ok', 'paused_slots': slots})

    @app.route('/api/unpause_all_meic', methods=['POST'])
    def unpause_all_meic():
        slots = [f'{lot}_{side}' for lot in tranche_lots for side in tranche_sides]
        root = os.path.abspath(os.path.join(trades_dir, '..'))
        try:
            from blocks.session.bootstrap import bootstrap_meic_session_if_missing
            from blocks.session.plan import load_meic_session_today

            bootstrap_meic_session_if_missing(root)
            plan = load_meic_session_today(root)
            if plan:
                for row in plan.rows:
                    plan.update_row(row.slot_key, paused=False)
                plan.save()
        except Exception:
            pass

        return jsonify({'status': 'ok', 'unpaused_slots': slots})

    @app.route('/api/manual_spread/scan', methods=['POST'])
    def manual_spread_scan():
        data = request.get_json(force=True) or {}
        try:
            broker = get_shared_broker()
            result = ms_entry.scan_spreads(
                broker,
                side=data.get('side', 'P'),
                expiry=data.get('expiry', ''),
                spread_width=int(data.get('spread_width', ms_config.DEFAULT_SPREAD_WIDTH)),
                target_credit=float(data.get('target_credit', ms_config.DEFAULT_TARGET_CREDIT)),
                max_results=int(data.get('max_results', ms_config.SCAN_MAX_RESULTS)),
            )
            return jsonify(result)
        except Exception as exc:
            log.exception('manual spread scan failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/place', methods=['POST'])
    def manual_spread_place():
        data = request.get_json(force=True) or {}
        try:
            from blocks.session.manual_helpers import dispatch_manual_place

            side = data.get('side', 'P')
            short_strike = int(data['short_strike'])
            long_strike = int(data['long_strike'])
            limit_credit = float(data['limit_credit'])
            quantity = int(data.get('quantity', ms_config.DEFAULT_QUANTITY))
            expiry = data.get('expiry', '')

            result, code = dispatch_manual_place(
                os.path.abspath(os.path.join(trades_dir, '..')),
                launcher_active=launcher_active(),
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
            if code == 200:
                _push_update()
            return jsonify(result), code
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        except Exception as exc:
            log.exception('manual spread place failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/modify', methods=['POST'])
    def manual_spread_modify():
        data = request.get_json(force=True) or {}
        filename = data.get('filename', '')
        if not filename:
            return jsonify({'status': 'error', 'error': 'filename required'}), 400
        try:
            broker = get_shared_broker()
            result = ms_entry.modify_spread(
                broker,
                filename=filename,
                new_limit_credit=float(data['new_limit_credit']),
            )
            code = 200 if result.get('status') == 'modified' else 400
            if code == 200:
                _push_update()
            return jsonify(result), code
        except (KeyError, TypeError, ValueError) as exc:
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        except Exception as exc:
            log.exception('manual spread modify failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/cancel', methods=['POST'])
    def manual_spread_cancel():
        data = request.get_json(force=True) or {}
        filename = data.get('filename', '')
        if not filename:
            return jsonify({'status': 'error', 'error': 'filename required'}), 400
        try:
            broker = get_shared_broker()
            result = ms_entry.cancel_spread(broker, filename=filename)
            code = 200 if result.get('status') == 'cancelled' else 400
            if code == 200:
                _push_update()
            return jsonify(result), code
        except Exception as exc:
            log.exception('manual spread cancel failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/close', methods=['POST'])
    def manual_spread_close():
        data = request.get_json(force=True) or {}
        filename = data.get('filename', '')
        if not filename:
            return jsonify({'status': 'error', 'error': 'filename required'}), 400
        cmd_dir = commands_dir_for_filename(filename, trades_dir)
        cmd_path = os.path.join(cmd_dir, f'{filename}.close.json')
        with open(cmd_path, 'w', encoding='utf-8') as f:
            json.dump({
                'action': 'close',
                'close_mechanism': 'manual_close',
                'ts': central_now_str(),
                'source': 'dashboard',
            }, f)
        _push_update()
        return jsonify({'status': 'close_command_written', 'filename': filename})
