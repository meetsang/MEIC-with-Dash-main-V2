"""TastyTrade spread pricing and opening (thin tranche path)."""
from __future__ import annotations

import os
import time

import meic0dte.app.config as config
import meic0dte.app.utilities as util
from blocks.entry.config import CreditEntryConfig
from blocks.entry.credit_spread import CreditSpreadEntry
from common.mqtt_prices import register_symbols_and_wait
from common.symbols import build_schwab_symbol, build_tastytrade_symbol, to_tastytrade
from blocks.entry.spread_scan import pick_meic_candidate
from blocks.stop import state as state_mod


def _meic_entry(broker, log) -> CreditSpreadEntry:
    return CreditSpreadEntry(broker, CreditEntryConfig.from_meic_config(), log=log)


def get_open_spread_price_tt(broker, opt_type: str, lot: str, log):
    """Find suitable spread using TastyTrade REST market data (no streamer)."""
    entry = _meic_entry(broker, log)
    expiry = util.get_expiration_date(log)
    candidates = entry.scan_for_meic(opt_type, expiry, lot)
    if not candidates:
        raise util.TerminateRequest(
            f'{lot} {opt_type}: no suitable credit found on TastyTrade'
        )
    pick = pick_meic_candidate(candidates)
    if pick is None:
        raise util.TerminateRequest(
            f'{lot} {opt_type}: strike overlap — could not shift to clear: '
            f'{candidates[0].overlap_warning}'
        )
    return (
        pick.short_symbol, pick.long_symbol, pick.market_credit,
        pick.short_strike, pick.long_strike,
    )


def open_spread_at_strikes_tt(
    broker,
    opt_type: str,
    quantity: int,
    lot: str,
    log,
    expiry_yymmdd: str,
    short_strike: int,
    long_strike: int,
    credit: float | None = None,
):
    """Place a credit spread at explicit expiry/strikes (adhoc + integration tests)."""
    short_symbol = build_tastytrade_symbol(expiry_yymmdd, opt_type, short_strike)
    long_symbol = build_tastytrade_symbol(expiry_yymmdd, opt_type, long_strike)

    if credit is None:
        from common.symbols import build_schwab_symbol

        register_symbols_and_wait(
            [
                build_schwab_symbol(expiry_yymmdd, opt_type, short_strike),
                build_schwab_symbol(expiry_yymmdd, opt_type, long_strike),
            ],
            lot,
            log,
        )
        prices = broker.get_option_prices([short_symbol, long_symbol])
        short_p = prices.get(to_tastytrade(short_symbol))
        long_p = prices.get(to_tastytrade(long_symbol))
        if short_p is None or long_p is None:
            missing = []
            if short_p is None:
                missing.append(short_symbol)
            if long_p is None:
                missing.append(long_symbol)
            raise util.TerminateRequest(
                f'{lot} {opt_type}: could not quote {", ".join(missing)}'
            )
        raw_credit = short_p - long_p
        rounded = round(raw_credit / 0.05) * 0.05
        if rounded > raw_credit:
            rounded -= config.OPEN_PRICE_ADJ
        credit = round(rounded, 2)
        log.info(
            'Quoted %s - %.2f | %s - %.2f = credit %.2f',
            short_symbol, short_p, long_symbol, long_p, credit,
        )

    log.info(
        'Placing %s spread %s/%s exp=%s qty=%d credit=%.2f',
        opt_type, short_strike, long_strike, expiry_yymmdd, quantity, credit,
    )
    result = broker.place_spread_order(short_symbol, long_symbol, quantity, credit)
    if not result.success:
        raise util.TerminateRequest(f'Open order failed: {result.message}')
    return short_symbol, long_symbol, result.order_id, credit, short_strike, long_strike


def open_spread_tt(broker, count: int, opt_type: str, quantity: int, lot: str, log):
    entry = _meic_entry(broker, log)
    expiry = util.get_expiration_date(log)
    candidates = entry.scan_for_meic(opt_type, expiry, lot)
    if not candidates:
        raise util.TerminateRequest(f'{lot} {opt_type}: no suitable credit found on TastyTrade')
    pick = pick_meic_candidate(candidates)
    if pick is None:
        raise util.TerminateRequest(
            f'{lot} {opt_type}: strike overlap — could not shift to clear: '
            f'{candidates[0].overlap_warning}'
        )
    log.info('Attempt %d: placing spread credit=%.2f', count, pick.market_credit)
    return entry.open_candidate(pick, opt_type, quantity, lot)


def write_pending_trade_state(
    *,
    lot: str,
    opt_type: str,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    target_quantity: int,
    open_order_id: str,
    limit_credit: float,
    strategy: str = 'MEIC_IC',
    active_directory: str | None = None,
    existing_path: str | None = None,
    entry_ts: str | None = None,
    reason: str = 'placed',
    on_unfilled_step: str | None = None,
) -> str:
    """Write handshake JSON immediately after order place (before fills)."""
    from blocks.entry.handshake import write_credit_spread_handshake

    return write_credit_spread_handshake(
        lot=lot,
        side=opt_type,
        short_symbol=short_symbol,
        long_symbol=long_symbol,
        short_strike=short_strike,
        long_strike=long_strike,
        quantity=target_quantity,
        open_order_id=open_order_id,
        limit_credit=limit_credit,
        strategy=strategy,
        active_directory=active_directory,
        existing_path=existing_path,
        entry_ts=entry_ts,
        reason=reason,
        on_unfilled_step=on_unfilled_step,
    )


def sync_trade_state_from_broker(broker, json_path: str, log) -> dict:
    """Refresh JSON from broker using open_order_id."""
    state, _ok, _detail = sync_entry_trade_state_from_broker(broker, json_path, log)
    return state


def sync_entry_trade_state_from_broker(broker, json_path: str, log) -> tuple[dict, bool, str]:
    """Entry fill poll — direct one-order status; returns (state, visibility_ok, detail)."""
    from blocks.stop.fill_sync import apply_order_result_to_state
    from brokers.tastytrade_broker import BrokerCooldownActive
    from common.rest_probe import classify_rest_exception
    from common.trading_gate import latch_new_risk

    state = state_mod.load_state(json_path)
    oid = state.get('open_order_id')
    if not oid:
        return state, True, ''

    try:
        get_direct = getattr(broker, 'get_order_status_direct', None)
        if get_direct is not None:
            result = get_direct(str(oid), priority='HIGH', op='entry_open_order_status')
        else:
            result = broker.get_order_status(str(oid), priority='HIGH', op='entry_open_order_status')
    except BrokerCooldownActive as exc:
        detail = str(exc)
        _mark_visibility_unknown(state, detail)
        state_mod.save_state(json_path, state)
        latch_new_risk('rest_rate_limited_during_entry', source=json_path, detail=detail, rest_status='rate_limited')
        log.warning('Entry status skipped (cooldown) order=%s — visibility frozen', oid)
        return state, False, detail
    except Exception as exc:
        status, _http = classify_rest_exception(exc)
        detail = str(exc)
        _mark_visibility_unknown(state, detail)
        state_mod.save_state(json_path, state)
        latch_new_risk(f'rest_{status}_during_entry', source=json_path, detail=detail, rest_status=status)
        log.warning('Entry status failed order=%s — visibility frozen: %s', oid, exc)
        return state, False, detail

    if not result.success:
        msg = (result.message or '').lower()
        if any(tok in msg for tok in ('429', 'cooldown', 'rate limit', 'unauthorized', 'timeout')):
            status = 'rate_limited' if '429' in msg or 'rate limit' in msg else 'unknown'
            if '401' in msg or 'unauthorized' in msg:
                status = 'auth_failed'
            if 'timeout' in msg:
                status = 'unavailable'
            _mark_visibility_unknown(state, result.message or '')
            state_mod.save_state(json_path, state)
            latch_new_risk(
                f'rest_{status}_during_entry',
                source=json_path,
                detail=result.message or '',
                rest_status=status,
            )
            log.warning('Entry status rejected order=%s — visibility frozen: %s', oid, result.message)
            return state, False, result.message or ''

    apply_order_result_to_state(state, result)
    state_mod.save_state(json_path, state)
    log.info(
        'Order %s sync: filled %s/%s status=%s',
        oid,
        state.get('filled_quantity'),
        state.get('quantity'),
        state_mod.section(state, 'open_order').get('status'),
    )
    return state, True, ''


def _mark_visibility_unknown(state: dict, detail: str) -> None:
    oo = state_mod.section(state, 'open_order')
    oo['status'] = 'visibility_unknown'
    oo['visibility_detail'] = detail
    state['entry_control'] = 'cooldown_blind'


def wait_and_sync_fill(broker, order_id: str, json_path: str, log, max_wait: int | None = None) -> dict:
    """Poll broker up to max_wait seconds; update handshake JSON each cycle."""
    max_wait = max_wait if max_wait is not None else config.FILL_WAIT_MAX
    deadline = time.time() + max_wait
    while time.time() < deadline:
        state = sync_trade_state_from_broker(broker, json_path, log)
        filled = int(state.get('filled_quantity') or 0)
        order_qty = int(state.get('quantity') or 0)
        ostatus = (state.get('open_order') or {}).get('status', 'working')
        if ostatus == 'filled' or (order_qty and filled >= order_qty):
            return state
        if ostatus in ('cancelled', 'canceled', 'rejected'):
            return state
        if filled > 0:
            return state
        time.sleep(config.FILL_WAIT)
    return sync_trade_state_from_broker(broker, json_path, log)


def wait_for_fill(broker, order_id: str, log, max_wait: int = 30) -> dict:
    """Poll until filled, cancelled, or timeout."""
    deadline = time.time() + max_wait
    while time.time() < deadline:
        result = broker.get_order_status(order_id)
        status = result.status.lower()
        log.info('Order %s status: %s', order_id, status)
        if status == 'filled':
            return {
                'orderId': order_id,
                'status': 'FILLED',
                'filledQuantity': result.filled_quantity or 1,
                'price': result.filled_price,
            }
        if status == 'partial':
            return {
                'orderId': order_id,
                'status': 'PARTIAL',
                'filledQuantity': result.filled_quantity or 0,
                'price': result.filled_price,
            }
        if status in ('cancelled', 'rejected', 'canceled'):
            return {'orderId': order_id, 'status': status.upper()}
        time.sleep(config.FILL_WAIT)
    return {'orderId': order_id, 'status': 'WORKING'}


def write_trade_state(
    *,
    lot: str,
    opt_type: str,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    short_fill: float,
    long_fill: float,
    net_credit: float,
    quantity: int,
    open_order_id: str,
) -> str:
    """Write JSON state file for stop_monitor to pick up."""
    state_mod.ensure_dirs()
    filename = state_mod.state_filename('MEIC_IC', lot, opt_type, open_order_id=open_order_id)
    path = os.path.join(state_mod.active_dir(), filename)

    state = state_mod.create_new_state(
        strategy='MEIC_IC',
        lot=lot,
        side=opt_type,
        short_symbol=to_tastytrade(short_symbol),
        long_symbol=to_tastytrade(long_symbol),
        short_strike=short_strike,
        long_strike=long_strike,
        short_fill=short_fill,
        long_fill=long_fill,
        net_credit=net_credit,
        quantity=quantity,
        open_order_id=open_order_id,
    )
    state_mod.save_state(path, state)
    return path
