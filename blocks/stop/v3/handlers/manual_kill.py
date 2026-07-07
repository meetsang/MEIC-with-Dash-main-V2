"""Condition 3 — manual close / killswitch (V3 §5.3)."""
from __future__ import annotations

import logging
import os

from blocks.stop import state as state_mod
from blocks.stop.fill_sync import stop_qty_for_state
from blocks.stop.v3.handlers.monitor_adapter import MonitorAdapter
from blocks.stop.v3.observability import timed_exit_step
from blocks.stop.v3.quotes import resolve_spread_close_debit
from blocks.stop.v3.recovery import (
    ensure_v3_exit_fields,
    mark_exit_error,
    mark_exit_progress,
    mark_exit_started,
    spread_close_preflight_blocked,
)
from blocks.stop.v3.trade_slot import save_slot

log = logging.getLogger(__name__)


class ManualKillHandler(MonitorAdapter):
    def __init__(self, slot, broker, prices, lane, alert_listener=None):
        self.slot = slot
        self.broker = broker
        self.prices = prices
        self.lane = lane
        self.alert_listener = alert_listener
        self._trade_id = os.path.basename(slot.path)

    def run(self, *, reason: str = 'manual_close') -> None:
        ensure_v3_exit_fields(self.slot.state, mechanism=reason)
        if not self.slot.state.get('exit_started_at'):
            mark_exit_started(self.slot.state, step='manual_kill_worker', mechanism=reason)

        def _pipeline() -> None:
            with timed_exit_step(
                path=self.slot.path,
                handler=reason,
                step='pipeline',
            ):
                mon = self.monitor()
                status = str(self.slot.state.get('status') or '')
                if status in ('closed', 'cancelled'):
                    log.info('manual_kill_skip_already_terminal path=%s status=%s', self.slot.path, status)
                    return
                if self.slot.state.get('exit_last_step') in ('spread_close_filled', 'finalized_closed'):
                    log.info('manual_kill_skip_already_finalized path=%s', self.slot.path)
                    return

                mark_exit_progress(self.slot.state, 'manual_kill_start')
                save_slot(self.slot)

                active = self.slot.state.get('active_stop') or {}
                oid = active.get('order_id')
                if oid:
                    mark_exit_progress(self.slot.state, 'cancel_stop')
                    save_slot(self.slot)
                    outcome = mon._cancel_stop_and_confirm(str(oid))
                    if outcome == 'filled':
                        result = self.broker.get_order_status(str(oid))
                        mon.handle_stop_order_update(active, broker_result=result)
                        self.sync_state_from_monitor()
                        save_slot(self.slot)
                        log.info(
                            'Manual kill: stop filled during cancel — Condition 2 path %s',
                            self.slot.path,
                        )
                        return
                    if outcome != 'cancelled':
                        mark_exit_error(self.slot.state, 'stop_cancel_failed', step='cancel_stop')
                        save_slot(self.slot)
                        log.error(
                            'Manual kill: stop %s not cancelled — aborting %s',
                            oid, self.slot.path,
                        )
                        return
                    self.slot.state['active_stop'] = None
                    self.slot.state['stop_quantity'] = 0
                    state_mod.append_stop_history(
                        self.slot.state,
                        action='cancelled',
                        order_id=str(oid),
                        price=active.get('stop_price') or active.get('limit_price'),
                        phase=active.get('phase', 1),
                        reason=f'spread_close_cancel:{reason}',
                        spx_price_at_event=self.prices.get_spx(),
                    )
                    mon.state = self.slot.state

                if self.slot.state.get('spread_close_order_id'):
                    mark_exit_progress(self.slot.state, 'poll_spread_close')
                    save_slot(self.slot)
                    mon._poll_spread_close()
                    self.sync_state_from_monitor()
                    save_slot(self.slot)
                    return

                mark_exit_progress(self.slot.state, 'resolve_quotes')
                save_slot(self.slot)
                quote = resolve_spread_close_debit(self.slot.state, self.prices, self.broker)
                if quote is None:
                    mark_exit_error(self.slot.state, 'missing_quotes', step='resolve_quotes')
                    save_slot(self.slot)
                    log.critical(
                        'Manual kill missing quotes for %s / %s — close_only_mode retained',
                        self.slot.state['short_leg']['symbol'],
                        self.slot.state['long_leg']['symbol'],
                    )
                    return

                qty = stop_qty_for_state(self.slot.state)
                short_sym = self.slot.state['short_leg']['symbol']
                long_sym = self.slot.state['long_leg']['symbol']

                block_reason = spread_close_preflight_blocked(
                    self.broker,
                    self.slot.state,
                    short_sym=short_sym,
                    long_sym=long_sym,
                    qty=qty,
                )
                if block_reason == 'existing_close_order':
                    mark_exit_progress(self.slot.state, 'poll_spread_close')
                    save_slot(self.slot)
                    mon._poll_spread_close()
                    self.sync_state_from_monitor()
                    save_slot(self.slot)
                    return
                if block_reason:
                    log.error(
                        'manual_kill_skip_not_closable path=%s position_state=%s',
                        self.slot.path,
                        block_reason,
                    )
                    mark_exit_error(
                        self.slot.state,
                        f'preflight_{block_reason}',
                        step='preflight',
                    )
                    save_slot(self.slot)
                    return

                mark_exit_progress(self.slot.state, f'place_spread_close:{quote.source}')
                save_slot(self.slot)

                result = self.broker.place_spread_close_order(
                    short_sym, long_sym, qty, quote.debit,
                )
                if not result.success:
                    mark_exit_error(self.slot.state, 'spread_close_rejected', step='place_spread_close')
                    save_slot(self.slot)
                    log.error('Manual kill spread close failed: %s', result.message)
                    return

                if not self.slot.state.get('close_mechanism'):
                    self.slot.state['close_mechanism'] = reason

                mon.state = self.slot.state
                if str(result.status).lower() == 'filled':
                    mon._apply_spread_close_fill(result)
                    self.sync_state_from_monitor()
                    mark_exit_progress(self.slot.state, 'spread_close_filled')
                    save_slot(self.slot)
                    log.info('Manual kill spread close filled order=%s', result.order_id)
                    return

                self.slot.state['spread_close_order_id'] = result.order_id
                self.slot.state['status'] = 'closing'
                mark_exit_progress(self.slot.state, 'spread_close_working')
                save_slot(self.slot)
                log.info(
                    'Manual kill spread close working order=%s debit=%.2f qty=%s source=%s',
                    result.order_id,
                    quote.debit,
                    qty,
                    quote.source,
                )

        self.lane.run(self._trade_id, _pipeline)
        self.slot.exit_job_id = None

