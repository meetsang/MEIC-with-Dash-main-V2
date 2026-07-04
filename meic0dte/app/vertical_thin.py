"""Thin tranche vertical spread — open only, delegate close to stop_monitor.

Deprecated for production MEIC entry (session CSV + entry monitor). Kept for
``MEIC_USE_LEGACY_ENTRY=1``, integration sessions with non-CSV lots, and rollback.
"""
import os
import time
import threading

import meic0dte.app.config as config
import meic0dte.app.utilities as util
from common.broker_factory import get_broker
from common.integration_report import append_event
from common.streamer_symbols import register_spread_symbols
from meic0dte.open import open_spread_tt
from blocks.stop import state as state_mod


def _integration_mode() -> bool:
    return os.environ.get('MEIC_INTEGRATION', '').lower() in ('1', 'true', 'yes')


def tranche(lot):
    print(f'{lot} MEIC Thin Tranche Started')
    threads = []
    for opt_type in ('P', 'C'):
        f_name = f'{lot}_{"put" if opt_type == "P" else "call"}.log'
        log = util.get_logger(opt_type, f_name)
        t = threading.Thread(target=vertical_spread_thin, args=(lot, opt_type, log))
        threads.append(t)

    for t in threads:
        t.start()
        time.sleep(5)
    for t in threads:
        t.join()
    print(f'{lot} MEIC Thin Tranche Ended')


def vertical_spread_thin(lot, opt_type, log):
    util.get_expiration_date(log)
    quantity = config.QUANTITY
    broker = get_broker()
    integration = _integration_mode()
    max_attempts = 1 if integration else 10

    count = 0
    entry_ts = state_mod.entry_ts_compact()
    trade_path = None
    while count < max_attempts:
        count += 1
        log.info('OPENING NEW ORDER (TastyTrade thin tranche)')
        short_symbol, long_symbol, open_order_id, credit, short_strike, long_strike = (
            open_spread_tt.open_spread_tt(broker, count, opt_type, quantity, lot, log)
        )

        retry_reason = 'placed' if trade_path is None else 'cancelled_for_chase'
        path = open_spread_tt.write_pending_trade_state(
            lot=lot,
            opt_type=opt_type,
            short_symbol=short_symbol,
            long_symbol=long_symbol,
            short_strike=short_strike,
            long_strike=long_strike,
            target_quantity=quantity,
            open_order_id=open_order_id,
            limit_credit=credit,
            existing_path=trade_path,
            entry_ts=entry_ts if trade_path is None else None,
            reason=retry_reason,
        )
        trade_path = path
        log.info(
            'Handshake JSON %s for order %s — stop_monitor syncs fills; no stop on place',
            path,
            open_order_id,
        )

        st = state_mod.load_state(path)
        register_spread_symbols(st, lot, log)

        state = open_spread_tt.wait_and_sync_fill(
            broker, open_order_id, path, log, max_wait=config.FILL_WAIT_MAX
        )
        filled = int(state.get('filled_quantity') or 0)
        ostatus = (state.get('open_order') or {}).get('status', 'working')

        append_event({
            'event': 'open_order',
            'lot': lot,
            'side': opt_type,
            'order_id': open_order_id,
            'status': ostatus.upper() if ostatus != 'partial' else 'PARTIAL',
            'filled_quantity': filled,
            'target_quantity': quantity,
            'short_symbol': short_symbol,
            'long_symbol': long_symbol,
            'credit': credit,
            'short_strike': short_strike,
            'long_strike': long_strike,
            'json_path': path,
        })

        if integration:
            log.info(
                'Integration: order %s filled %s/%s — stop_monitor handles further fills/stops',
                open_order_id,
                filled,
                quantity,
            )
            return

        if filled >= quantity or state_mod.section(state, 'open_order').get('fully_filled'):
            log.info('Fully filled %s/%s — stop_monitor will place stop', filled, quantity)
            return

        if filled > 0:
            log.info('Partial fill %s/%s — stop_monitor will resize stop as fills arrive', filled, quantity)
            return

        if ostatus in ('cancelled', 'canceled', 'rejected'):
            log.info('Order %s %s — retrying', open_order_id, ostatus)
            continue

        log.info('No fill after %ss — cancelling %s and retrying', config.FILL_WAIT_MAX, open_order_id)
        broker.cancel_order(open_order_id)
        time.sleep(1)

    if not integration:
        raise util.TerminateRequest(f'{lot} {opt_type} open failed after max attempts')
