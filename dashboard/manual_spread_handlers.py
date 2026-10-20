"""Manual Spread dashboard API handlers."""
from __future__ import annotations

import json
import logging
import os

from flask import jsonify, request

from common.broker_factory import get_shared_broker, get_shared_broker_for_strategy
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
from common.symbols import parse_canonical
from common.option_ticks import round_spx_option_price

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
        is_butterfly = trade.get('structure_type') == 'butterfly'
        short_leg = trade.get('short_leg', {})
        long_leg = trade.get('long_leg', {})
        phases = trade.get('phases', {})

        if is_butterfly:
            short_sym = ''
            long_sym = ''
            body_sym = trade.get('body_symbol') or ''
            canonical = parse_canonical(body_sym) if body_sym else None
            expiry = canonical[0] if canonical else (entry.get('expiry') or trade.get('expiry') or '')
            structure_type = trade.get('structure_type') or entry.get('structure_type') or 'butterfly'
            limit_credit = float(
                entry.get('limit_credit')
                or entry.get('net_credit')
                or entry.get('limit_debit')
                or entry.get('net_debit')
                or 0
            )
            quantity = int(trade.get('quantity') or 1)
            filled = int(trade.get('filled_quantity') or 0)
            short_fill = 0.0
            long_fill = 0.0
            short_close = None
            long_close = None
            cur_short = 0.0
            cur_long = 0.0
            cur_spread = 0.0
            live_pnl = 0.0
            pnl_class = 'text-muted'
            pnl_frozen = False
            exit_credit = None
            exit_short = None
            lot = trade.get('lot', '')
            side = entry.get('side', '')
            slot_key = f'{lot}_{side}' if lot and side else ''
            strike_label = f"{trade.get('wing_low')}/{trade.get('body_strike')}/{trade.get('wing_high')}"
            entry_label = ''
            if status == 'pending_fill':
                entry_label = f'lim {limit_credit:.2f} ({filled}/{quantity})'
            elif filled > 0:
                px = float(entry.get('net_credit') or entry.get('net_debit') or limit_credit)
                entry_label = f'{px:.2f} BF'
            rows.append({
                'lot': lot,
                'side': side,
                'slot_key': slot_key,
                'stop_multiplier': 0,
                'short_strike': strike_label,
                'long_strike': entry.get('structure_type', 'butterfly'),
                'limit_credit': limit_credit,
                'filled': filled,
                'quantity': filled if filled else quantity,
                'entry_credit': limit_credit if not filled else float(
                    entry.get('net_credit') or entry.get('net_debit') or limit_credit
                ),
                'entry_label': entry_label,
                'exit_label': '',
                'live_pnl': live_pnl,
                'pnl_class': pnl_class,
                'pnl_frozen': pnl_frozen,
                'slippage': None,
                'slippage_label': '',
                'cur_short': cur_short,
                'cur_long': cur_long,
                'cur_spread': cur_spread,
                'phase': 'hold' if status == 'open' else '',
                'stop_label': 'hold to expiry',
                'stop_order_id': '',
                'state': 'working' if status == 'pending_fill' else slot_state_fn(
                    status, trade.get('close_mechanism'), strategy=entry.get('strategy'), trade=trade,
                ),
                'status': status,
                'entry_quote_source_label': quote_source_label(trade),
                'breach_readiness_label': '',
                '_filename': trade.get('_filename', ''),
                'expiry': expiry,
                'structure_type': structure_type,
                'entry_time': trade_entry_time_iso(trade),
                'exit_time': trade_exit_time_iso(trade),
            })
            if status in ('open', 'closing', 'pending_fill'):
                open_count += 1
            continue

        short_sym = short_leg.get('symbol', '')
        long_sym = long_leg.get('symbol', '')
        canonical = parse_canonical(short_sym) if short_sym else None
        expiry = canonical[0] if canonical else (entry.get('expiry') or trade.get('expiry') or '')
        structure_type = trade.get('structure_type') or entry.get('structure_type') or ''
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
        if status in ('open', 'closing', 'closed'):
            eff_qty = filled if filled > 0 else int(trade.get('holding_quantity') or trade.get('quantity') or trade.get('initial_quantity') or 1)
            live_pnl, exit_credit, exit_short, pnl_frozen = trade_pnl_fn(
                net, eff_qty,
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
        elif exit_credit is not None:
            exit_label = f'{exit_credit:.2f}'

        slip_usd = None
        if status == 'closed' or short_close is not None:
            slip_usd = slippage_dollars(trade)

        active_stop = _resolve_active_stop(trade)
        stop_order_id = active_stop.get('order_id') or ''
        stop_mult = stop_multiplier_for_state(trade)
        stop_px = active_stop.get('stop_price') or active_stop.get('stop_trigger_debit')
        if stop_px is None and net and stop_mult:
            stop_px = round_spx_option_price(net * stop_mult)
        is_split = bool(trade.get('is_scale_out_split'))
        parent_lot = str(trade.get('parent_lot') or '')
        is_oco_stop = active_stop.get('type') in ('SPREAD_OCO', 'OCO') or is_split
        breach_fields = breach_display_fields(
            trade,
            live_short=live_price_fn(short_sym),
            live_long=live_price_fn(long_sym),
            trade_status=status,
        )
        lot = trade.get('lot', '')
        side = entry.get('side', '')
        slot_key = f'{lot}_{side}' if lot and side else ''
        pt_pct = trade.get('profit_target_pct')
        if pt_pct is None and isinstance(trade.get('exit_config'), dict):
            pt_pct = trade.get('exit_config', {}).get('profit_target_pct')
        if pt_pct is None and isinstance(trade.get('plan'), dict):
            pt_pct = trade.get('plan', {}).get('profit_target_pct')
        if pt_pct is None:
            pt_pct = 0.50
        else:
            pt_pct = float(pt_pct)

        rows.append({
            'lot': lot,
            'side': side,
            'slot_key': slot_key,
            'stop_multiplier': stop_mult,
            'profit_target_pct': pt_pct,
            'short_strike': short_leg.get('strike', ''),
            'long_strike': long_leg.get('strike', ''),
            'limit_credit': limit_credit,
            'filled': filled,
            'quantity': filled if filled else quantity,
            'initial_quantity': trade.get('initial_quantity') or quantity,
            'holding_quantity': int(trade.get('holding_quantity', (filled if status != 'closed' else 0))),
            'closed_quantity': int(trade.get('closed_quantity', (sum(int(s.get('quantity', 0) or 0) for s in (trade.get('scale_outs') or [])) + (quantity if status == 'closed' else 0)))),
            'scale_outs': trade.get('scale_outs') or [],
            'scaled_qty': sum(int(s.get('quantity', 0) or 0) for s in (trade.get('scale_outs') or [])),
            'close': trade.get('close') or {},
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
                'OCO (PT/SL)'
                if is_oco_stop and status == 'open'
                else (
                    phase_display_fn(phases, stop_multiplier_for_state(trade))
                    if status == 'open'
                    else ''
                )
            ),
            'stop_label': stop_label_fn(active_stop),
            'stop_order_id': stop_order_id,
            'stop_price': float(stop_px) if stop_px is not None else None,
            'is_scale_out_split': is_split,
            'parent_lot': parent_lot,
            'state': row_state,
            'status': status,
            'entry_quote_source_label': quote_source_label(trade),
            'breach_readiness_label': breach_readiness_label(trade),
            '_filename': trade.get('_filename', ''),
            'expiry': expiry,
            'structure_type': structure_type,
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

    try:
        from blocks.session.plan import load_manual_session_today
        import os

        root_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        session_plan = load_manual_session_today(root_dir)

        if session_plan:
            known_lots = {r.get('lot') for r in rows if r.get('lot')}
            known_slot_keys = {r.get('slot_key') for r in rows if r.get('slot_key')}
            for srow in session_plan.rows:
                if srow.skip or srow.paused or srow.state in ('cancelled', 'deleted'):
                    continue
                if srow.lot in known_lots or srow.slot_key in known_slot_keys:
                    continue
                st = srow.state or 'entering'
                if st in ('entering', 'placing', 'waiting_rest_probe', 'blocked_rest', 'failed', 'pending'):
                    err = getattr(srow, 'error', '') or ''
                    if st == 'waiting_rest_probe':
                        stop_lbl = 'Waiting REST probe'
                    elif st in ('entering', 'placing'):
                        stop_lbl = 'Submitting to broker...'
                    elif st == 'failed':
                        stop_lbl = f'Failed: {err}' if err else 'Failed'
                    elif st == 'blocked_rest':
                        stop_lbl = f'Gate blocked: {err}' if err else 'Gate blocked'
                    else:
                        stop_lbl = err or 'Queued'

                    lim = float(srow.limit_credit or 0)
                    qty = int(srow.quantity or 1)
                    pt_val = getattr(srow, 'profit_target_pct', 0.50)
                    if pt_val is None:
                        pt_val = 0.50
                    rows.append({
                        'lot': srow.lot,
                        'side': srow.side,
                        'slot_key': srow.slot_key,
                        'stop_multiplier': srow.stop_multiplier or 0,
                        'profit_target_pct': float(pt_val),
                        'short_strike': str(srow.short_strike),
                        'long_strike': str(srow.long_strike),
                        'limit_credit': lim,
                        'filled': 0,
                        'quantity': qty,
                        'initial_quantity': qty,
                        'holding_quantity': qty,
                        'closed_quantity': 0,
                        'scale_outs': [],
                        'scaled_qty': 0,
                        'close': {},
                        'entry_credit': lim,
                        'entry_label': f'lim {lim:.2f} ({st})',
                        'exit_label': '',
                        'live_pnl': 0.0,
                        'pnl_class': 'text-muted',
                        'pnl_frozen': False,
                        'slippage': None,
                        'slippage_label': '',
                        'cur_short': 0.0,
                        'cur_long': 0.0,
                        'cur_spread': 0.0,
                        'phase': 'queued',
                        'stop_label': stop_lbl,
                        'stop_order_id': '',
                        'state': st,
                        'status': st,
                        'entry_quote_source_label': '',
                        'breach_readiness_label': '',
                        '_filename': '',
                        'expiry': srow.expiry or '',
                        'structure_type': getattr(srow, 'structure_type', '') or 'vertical',
                        'entry_time': None,
                        'exit_time': None,
                    })
                    if st in ('entering', 'placing', 'waiting_rest_probe'):
                        open_count += 1
    except Exception as exc:
        log.warning('Failed to load session rows in build_manual_trades: %s', exc)

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

    @app.route('/api/manual_spread/chain_status', methods=['GET'])
    def manual_spread_chain_status():
        from common.chain_snapshot import snapshot_status

        expiry = request.args.get('expiry', '')
        if not expiry:
            return jsonify({'status': 'error', 'error': 'expiry required'}), 400
        try:
            expiry_yymmdd = ms_entry.parse_expiry(expiry)
        except ValueError as exc:
            return jsonify({'status': 'error', 'error': str(exc)}), 400
        return jsonify({'status': 'ok', **snapshot_status(expiry_yymmdd)})

    def _resolve_manual_broker(paper: bool):
        if (
            hasattr(get_shared_broker, 'mock_calls')
            or hasattr(get_shared_broker, 'assert_called')
            or type(get_shared_broker).__module__.startswith('unittest.mock')
        ):
            return get_shared_broker()
        return get_shared_broker_for_strategy(ms_config.STRATEGY, paper=paper)

    @app.route('/api/manual_spread/load_chain', methods=['POST'])
    def manual_spread_load_chain():
        data = request.get_json(force=True) or {}
        expiry = data.get('expiry', '')
        if not expiry:
            return jsonify({'status': 'error', 'error': 'expiry required'}), 400
        chain_band = int(data.get('chain_band', ms_config.DEFAULT_CHAIN_BAND))
        try:
            from common.broker_factory import is_strategy_paper
            paper = is_strategy_paper(ms_config.STRATEGY)
            broker = _resolve_manual_broker(paper)
            result = ms_entry.load_chain(broker, expiry=expiry, chain_band=chain_band)
            return jsonify(result)
        except Exception as exc:
            log.exception('manual spread load_chain failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/clear_chain', methods=['POST'])
    def manual_spread_clear_chain():
        data = request.get_json(force=True) or {}
        expiry = data.get('expiry', '')
        if not expiry:
            return jsonify({'status': 'error', 'error': 'expiry required'}), 400
        try:
            result = ms_entry.clear_chain(expiry)
            return jsonify(result)
        except Exception as exc:
            log.exception('manual spread clear_chain failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/scan', methods=['POST'])
    def manual_spread_scan():
        data = request.get_json(force=True) or {}
        try:
            from common.broker_factory import is_strategy_paper
            paper = is_strategy_paper(ms_config.STRATEGY)
            broker = _resolve_manual_broker(paper)
            structure_type = (data.get('structure_type') or '').strip().lower()
            target = float(
                data.get('target_credit')
                or data.get('target_price')
                or (
                    ms_config.DEFAULT_BF_TARGET_SELL
                    if structure_type.startswith('sell_')
                    else ms_config.DEFAULT_BF_TARGET_BUY if structure_type.startswith('buy_') else ms_config.DEFAULT_TARGET_CREDIT
                )
            )
            chain_band = int(data['chain_band']) if data.get('chain_band') is not None else None
            otm_max = int(data['otm_max']) if data.get('otm_max') is not None else None
            result = ms_entry.scan_spreads(
                broker,
                side=data.get('side', 'P'),
                expiry=data.get('expiry', ''),
                spread_width=int(data.get('spread_width', ms_config.DEFAULT_SPREAD_WIDTH)),
                target_credit=target,
                max_results=int(data.get('max_results', ms_config.SCAN_MAX_RESULTS)),
                structure_type=structure_type,
                wing_half=int(data.get('wing_half', ms_config.DEFAULT_BF_WING_HALF)),
                otm_min=int(data['otm_min']) if data.get('otm_min') is not None else None,
                otm_max=otm_max,
                chain_band=chain_band,
                paper=paper,
            )
            return jsonify(result)
        except Exception as exc:
            from common.chain_snapshot import ChainSnapshotNotLoaded

            if isinstance(exc, ChainSnapshotNotLoaded):
                return jsonify({'status': 'error', 'error': str(exc), 'chain_required': True}), 400
            log.exception('manual spread scan failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/load_and_scan', methods=['POST'])
    def manual_spread_load_and_scan():
        data = request.get_json(force=True) or {}
        expiry = (data.get('expiry') or '').strip()
        if not expiry:
            return jsonify({'status': 'error', 'error': 'expiry required'}), 400
        chain_band = int(data.get('chain_band', ms_config.DEFAULT_CHAIN_BAND))
        try:
            from common.broker_factory import is_strategy_paper
            paper = is_strategy_paper(ms_config.STRATEGY)
            broker = _resolve_manual_broker(paper)
            chain_result = ms_entry.load_chain(broker, expiry=expiry, chain_band=chain_band)

            structure_type = (data.get('structure_type') or '').strip().lower()
            target = float(
                data.get('target_credit')
                or data.get('target_price')
                or (
                    ms_config.DEFAULT_BF_TARGET_SELL
                    if structure_type.startswith('sell_')
                    else ms_config.DEFAULT_BF_TARGET_BUY if structure_type.startswith('buy_') else ms_config.DEFAULT_TARGET_CREDIT
                )
            )
            otm_max = int(data['otm_max']) if data.get('otm_max') is not None else None
            scan_result = ms_entry.scan_spreads(
                broker,
                side=data.get('side', 'P'),
                expiry=expiry,
                spread_width=int(data.get('spread_width', ms_config.DEFAULT_SPREAD_WIDTH)),
                target_credit=target,
                max_results=int(data.get('max_results', ms_config.SCAN_MAX_RESULTS)),
                structure_type=structure_type,
                wing_half=int(data.get('wing_half', ms_config.DEFAULT_BF_WING_HALF)),
                otm_min=int(data['otm_min']) if data.get('otm_min') is not None else None,
                otm_max=otm_max,
                chain_band=chain_band,
                paper=paper,
            )
            scan_result['chain'] = chain_result
            return jsonify(scan_result)
        except Exception as exc:
            log.exception('manual spread load_and_scan failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/place', methods=['POST'])
    def manual_spread_place():
        root = os.path.abspath(os.path.join(trades_dir, '..'))
        from common.daily_overrides import get_overrides
        ovr = get_overrides('MANUAL_SPREAD', root)
        if ovr.get('enabled') is False:
            return jsonify({'status': 'error', 'error': 'Manual Spread strategy is paused for today. Enable it in strategy controls to trade.'}), 400

        data = request.get_json(force=True) or {}
        try:
            from blocks.session.manual_helpers import dispatch_manual_place

            structure_type = (data.get('structure_type') or '').strip().lower()
            if structure_type:
                body_strike = int(data['body_strike'])
                wing_half = int(data.get('wing_half', ms_config.DEFAULT_BF_WING_HALF))
                limit_price = float(data.get('limit_price') or data['limit_credit'])
                quantity = int(data.get('quantity', ms_config.DEFAULT_QUANTITY))
                expiry = data.get('expiry', '')
                from manual_spread.butterfly import side_for_structure_type

                result, code = dispatch_manual_place(
                    os.path.abspath(os.path.join(trades_dir, '..')),
                    launcher_active=launcher_active(),
                    side=side_for_structure_type(structure_type),
                    short_strike=body_strike,
                    long_strike=wing_half,
                    limit_credit=limit_price,
                    quantity=quantity,
                    expiry=expiry,
                    structure_type=structure_type,
                    wing_half=wing_half,
                    wait_for_launcher_sec=2.0,
                    **{k: data.get(k) for k in (
                        'profit_target_pct', 'on_unfilled', 'fill_wait_sec', 'max_attempts',
                    ) if k in data},
                )
                if code == 200:
                    _push_update()
                return jsonify(result), code

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
                wait_for_launcher_sec=2.0,
                **{k: data.get(k) for k in (
                    'stop_multiplier', 'profit_target_pct', 'on_unfilled', 'fill_wait_sec',
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
            broker = get_shared_broker_for_strategy('MANUAL_SPREAD')
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
            broker = get_shared_broker_for_strategy('MANUAL_SPREAD')
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

    @app.route('/api/manual_spread/scale_out', methods=['POST'])
    def manual_spread_scale_out():
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
            broker = get_shared_broker_for_strategy('MANUAL_SPREAD')
            root = os.path.abspath(os.path.join(trades_dir, '..'))
            result = scale_out_spread(
                broker,
                live_prices,
                filename,
                close_qty=int(qty),
                debit_limit=float(debit_limit) if debit_limit is not None and float(debit_limit) > 0 else None,
                stop_loss=float(stop_loss) if stop_loss is not None and float(stop_loss) > 0 else None,
                root=root,
            )
            code = 200 if result.get('status') == 'ok' else 400
            if code == 200:
                _push_update()
            return jsonify(result), code
        except Exception as exc:
            log.exception('manual spread scale out failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500

    @app.route('/api/manual_spread/duplicate', methods=['POST'])
    def manual_spread_duplicate():
        data = request.get_json(force=True) or {}
        filename = (data.get('filename') or '').strip()
        if not filename:
            return jsonify({'status': 'error', 'error': 'filename required'}), 400

        trade = ms_entry.find_trade_by_filename(filename)
        if not trade:
            return jsonify({'status': 'error', 'error': f'Trade {filename} not found'}), 404

        entry = trade.get('entry') or {}
        short_leg = trade.get('short_leg') or {}
        long_leg = trade.get('long_leg') or {}
        structure_type = (trade.get('structure_type') or entry.get('structure_type') or '').strip().lower()

        # Resolve expiry
        short_sym = short_leg.get('symbol', '') or trade.get('body_symbol', '')
        expiry = ''
        if short_sym:
            canonical = parse_canonical(short_sym)
            if canonical:
                expiry = canonical[0]
        if not expiry:
            expiry = str(entry.get('expiry') or trade.get('expiry') or '')

        # Resolve strikes & side
        side = (data.get('side') or entry.get('side') or 'P').upper()
        quantity = int(data.get('quantity') or trade.get('quantity') or ms_config.DEFAULT_QUANTITY)
        stop_mult = float(data.get('stop_multiplier') or stop_multiplier_for_state(trade) or 2.0)

        # Resolve credit: user override, or fallback to original
        if data.get('limit_credit') is not None:
            limit_credit = float(data['limit_credit'])
        else:
            limit_credit = float(entry.get('limit_credit') or entry.get('net_credit') or 0.55)

        pt_pct = data.get('profit_target_pct')
        if pt_pct is None:
            pt_pct = trade.get('profit_target_pct') or (trade.get('exit_config') or {}).get('profit_target_pct') or 0.50
        plan_fields = {
            'stop_multiplier': stop_mult,
            'profit_target_pct': float(pt_pct),
            'on_unfilled': data.get('on_unfilled') or trade.get('on_unfilled') or 'none',
            'fill_wait_sec': int(data.get('fill_wait_sec') or trade.get('plan', {}).get('fill_wait_sec') or 5),
            'max_attempts': int(data.get('max_attempts') or trade.get('plan', {}).get('max_attempts') or 1),
        }

        try:
            from blocks.session.manual_helpers import dispatch_manual_place

            if structure_type:
                body_strike = int(data.get('body_strike') or trade.get('body_strike') or short_leg.get('strike') or 0)
                wing_half = int(data.get('wing_half') or trade.get('wing_half') or ms_config.DEFAULT_BF_WING_HALF)
                from manual_spread.butterfly import side_for_structure_type

                result, code = dispatch_manual_place(
                    os.path.abspath(os.path.join(trades_dir, '..')),
                    launcher_active=launcher_active(),
                    side=side_for_structure_type(structure_type),
                    short_strike=body_strike,
                    long_strike=wing_half,
                    limit_credit=limit_credit,
                    quantity=quantity,
                    expiry=expiry,
                    structure_type=structure_type,
                    wing_half=wing_half,
                    wait_for_launcher_sec=2.0,
                    **plan_fields,
                )
            else:
                short_strike = int(data.get('short_strike') or short_leg.get('strike') or 0)
                long_strike = int(data.get('long_strike') or long_leg.get('strike') or 0)
                if not short_strike or not long_strike:
                    return jsonify({'status': 'error', 'error': 'Could not determine strikes from trade'}), 400

                result, code = dispatch_manual_place(
                    os.path.abspath(os.path.join(trades_dir, '..')),
                    launcher_active=launcher_active(),
                    side=side,
                    short_strike=short_strike,
                    long_strike=long_strike,
                    limit_credit=limit_credit,
                    quantity=quantity,
                    expiry=expiry,
                    wait_for_launcher_sec=2.0,
                    **plan_fields,
                )

            if code == 200:
                _push_update()
            return jsonify(result), code
        except Exception as exc:
            log.exception('manual spread duplicate failed')
            return jsonify({'status': 'error', 'error': str(exc)}), 500
