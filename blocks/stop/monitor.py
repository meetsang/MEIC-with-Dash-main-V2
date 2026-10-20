"""Per-spread stop monitor thread."""
from __future__ import annotations

import datetime
import json
import logging
import os
import queue
import threading
import time
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import meic0dte.app.config as app_config
from meic0dte.app.utilities import central_time
from brokers.base import BrokerBase, OrderResult
from blocks.stop import state as state_mod
from common.option_ticks import round_spx_option_price, step_down_spx_option_price
from blocks.stop.fill_sync import (
    stop_is_current,
    stop_order_fully_filled,
    stop_qty_for_state,
    sync_open_order,
)
from blocks.stop.mqtt_prices import MqttPriceCache
from common.broker_action_window import (
    MEIC_SPX_OPTIONS_RTH_ACTIONS,
    broker_actions_allowed_for_trade,
)
from common.broker_stopped_trading import is_instruments_stopped_trading_error
from common.market_hours import (
    MARKET_CLOSE_HOUR_CT,
    MARKET_CLOSE_MINUTE_CT,
    trade_past_0dte_close,
)
from blocks.stop.reconcile_policy import (
    is_working_stop_reconcile_due,
    schedule_next_working_stop_reconcile,
)
from common.rest_operations import (
    OPERATION_ALERT_CONFIRM,
    OPERATION_LONG_CLOSE_STATUS,
    OPERATION_SPREAD_CLOSE_STATUS,
    OPERATION_STOP_CANCEL,
    OPERATION_WORKING_STOP_RECONCILE,
    PRIORITY_HIGH,
    PRIORITY_LOW,
)
from blocks.stop.expiry_gate import try_settle_or_freeze_trade
from blocks.stop.stop_math import apply_two_x_thresholds, exchange_stop_limit_prices, exchange_stop_price, spread_breach_threshold, stop_multiplier_for_state
from blocks.stop.stop_ownership import has_ownership_conflict
from blocks.stop.v3.quotes import LegQuoteResult, resolve_leg_mid
from common.mqtt_prices import mqtt_cache_is_stale
from common.streamer_health import is_stale as streamer_prices_stale
from blocks.stop.stop_profile import StopProfile, resolve_stop_profile

if TYPE_CHECKING:
    from blocks.stop.alerts import AlertListener

log = logging.getLogger(__name__)

FAST_INTERVAL = 3       # seconds — breach check + long chase
LEGACY_LONG_CLOSE_FLOOR = 0.05

def _trades_dir() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    return os.path.join(root, 'trades')


def _trades_root_for_path(json_path: str) -> str:
    """Parent of active/ for the trade file (trades/ in V2 layout)."""
    norm = os.path.abspath(json_path)
    parts = norm.split(os.sep)
    if 'trades' in parts:
        idx = parts.index('trades')
        return os.sep.join(parts[:idx + 1])
    parent = os.path.dirname(norm)
    if os.path.isdir(os.path.join(parent, 'trades')):
        return os.path.join(parent, 'trades')
    return _trades_dir()
SLOW_INTERVAL = 10      # seconds — broker REST backup poll (stop fill detection)
LONG_CLOSE_DELAY_SEC = 30  # seconds — delay before starting long close chase
MAX_BREACH_THREADS = 12    # max concurrent breach handler threads (6 tranches x 2 sides)


def _integration_mode() -> bool:
    return os.environ.get('MEIC_INTEGRATION', '').lower() in ('1', 'true', 'yes')


class StopMonitor:
    """Manages one spread side (call or put) via JSON state + broker API."""

    def __init__(
        self,
        json_path: str,
        broker: BrokerBase,
        prices: MqttPriceCache,
        phases: Optional[List[PhaseBase]] = None,
        fill_queue: Optional[queue.Queue] = None,
        alert_listener: Optional['AlertListener'] = None,
        stop_profile: Optional[StopProfile] = None,
    ):
        self.json_path = json_path
        self.broker = broker
        self.prices = prices
        self.fill_queue = fill_queue or queue.Queue()
        self.alert_listener = alert_listener
        self.state = state_mod.load_state(json_path)
        self.stop_profile = stop_profile or resolve_stop_profile(self.state)
        self.long_close_delay_sec = self.stop_profile.long_close_delay_sec
        self.phases = sorted(phases or self.stop_profile.phases, key=lambda p: p.priority)
        self._stop_event = threading.Event()
        self._last_broker_sync: float = 0
        self.kill_switch = False
        self._breach_lock = threading.Lock()
        self._breach_active = False
        self._long_chase_active = False
        self._stop_place_backoff_until = 0.0
        self._stale_warned = False
        self._0dte_freeze_logged = False
        self._broker_window_pause_logged = False
        self._breach_missing_prices_logged = False
        self._breach_stale_logged = False
        self._breach_last_near_log = 0.0
        self._breach_last_near_spread: Optional[float] = None
        self._skip_dashboard_commands = False

    def _0dte_past_market_close(self) -> bool:
        return trade_past_0dte_close(
            self.state,
            os.path.basename(self.json_path),
        )

    def _log_0dte_freeze_once(self) -> None:
        if self._0dte_freeze_logged:
            return
        self._0dte_freeze_logged = True
        log.info(
            '0DTE past %02d:%02d CT — freezing broker actions for %s',
            MARKET_CLOSE_HOUR_CT,
            MARKET_CLOSE_MINUTE_CT,
            self.json_path,
        )

    def _broker_action_profile(self):
        return MEIC_SPX_OPTIONS_RTH_ACTIONS

    def _protection_incomplete(self) -> bool:
        if self.state.get('status') != 'open':
            return False
        if stop_qty_for_state(self.state) <= 0:
            return False
        return not stop_is_current(
            self.state,
            ownership_conflict=has_ownership_conflict(self.state),
        )

    def _apply_broker_pause_markers(self, reason: str) -> None:
        self.state['broker_actions_paused'] = True
        self.state['broker_actions_pause_reason'] = reason
        if self._protection_incomplete():
            self.state['stop_rearm_pending'] = True

    def _clear_broker_pause_markers(self) -> None:
        self.state.pop('broker_actions_paused', None)
        self.state.pop('broker_actions_pause_reason', None)

    def _log_broker_window_pause_once(self, reason: str) -> None:
        if self._broker_window_pause_logged:
            return
        self._broker_window_pause_logged = True
        log.info(
            'Broker actions paused (%s) for %s',
            reason,
            self.json_path,
        )

    def _maybe_rearm_after_window_open(self) -> None:
        if self.state.get('status') != 'open':
            return
        if not (self.state.get('stop_rearm_pending') or self._protection_incomplete()):
            return
        self._ensure_stop_for_filled_qty_unblocked()
        if stop_is_current(
            self.state,
            ownership_conflict=has_ownership_conflict(self.state),
        ):
            self.state.pop('stop_rearm_pending', None)

    def _apply_expiry_gate(self) -> str:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
        outcome, state = try_settle_or_freeze_trade(
            self.state,
            path=self.json_path,
            root=root,
        )
        if outcome != 'ok':
            self.state = state
            state_mod.section(self.state, 'recovery')
            state_mod.save_state(self.json_path, self.state)
            if outcome == 'settled':
                log.info('Expiry settlement closed %s', self.json_path)
            elif outcome == 'frozen':
                log.info('Expiry settlement pending — frozen %s', self.json_path)
        return outcome

    def _mark_instruments_stopped_trading(self, message: str) -> None:
        log.warning('Broker instruments stopped trading for %s: %s', self.json_path, message)
        self.state['broker_actions_disabled'] = True
        self.state['broker_actions_disabled_reason'] = 'instruments_stopped_trading'
        self.state['expiry_settlement_pending'] = True
        self.state['active_stop'] = None
        self.state['stop_quantity'] = 0
        self.state.pop('spread_close_order_id', None)
        self.state.pop('long_close_order_id', None)
        self._stop_place_backoff_until = 0.0
        state_mod.section(self.state, 'recovery')
        state_mod.save_state(self.json_path, self.state)

    def _broker_actions_blocked(self) -> bool:
        outcome = self._apply_expiry_gate()
        if outcome in ('settled', 'frozen', 'already_closed'):
            return True
        if self.state.get('status') == 'closed':
            return True
        if self.state.get('broker_actions_disabled'):
            return True
        allowed, reason = broker_actions_allowed_for_trade(
            self.state,
            filename=os.path.basename(self.json_path),
            profile=self._broker_action_profile(),
        )
        if allowed:
            self._broker_window_pause_logged = False
            self._clear_broker_pause_markers()
            return False
        if reason == 'expired_option':
            self._log_0dte_freeze_once()
            return True
        self._apply_broker_pause_markers(reason)
        self._log_broker_window_pause_once(reason)
        return True

    def _broker_actions_frozen(self) -> bool:
        """True when broker placement must not run (expiry cutoff or RTH window)."""
        return self._broker_actions_blocked()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self, poll_interval: float = 5.0) -> None:
        self._on_load()
        interval = min(poll_interval, FAST_INTERVAL)
        while not self._stop_event.is_set():
            self._drain_fill_queue()
            self._poll_once()
            if self.state.get('status') == 'closed':
                break
            self._stop_event.wait(interval)

    def _drain_fill_queue(self) -> None:
        if self._broker_actions_blocked():
            return
        while not self.fill_queue.empty():
            try:
                event = self.fill_queue.get_nowait()
                order_id = str(event.get('order_id', ''))
                active = self.state.get('active_stop') or {}
                if active.get('order_id') == order_id:
                    status = str(event.get('status', 'filled')).lower()
                    active['status'] = status
                    if status == 'filled':
                        result = self.broker.get_order_status(
                            order_id,
                            priority=PRIORITY_HIGH,
                            op=OPERATION_ALERT_CONFIRM,
                        )
                        self.handle_stop_order_update(active, broker_result=result)
            except queue.Empty:
                break

    def _on_load(self) -> None:
        rec = self.state.setdefault('recovery', {})
        rec['module_start_count'] = rec.get('module_start_count', 0) + 1
        rec['state_loaded_from_disk'] = True
        state_mod.save_state(self.json_path, self.state)

        from common.streamer_symbols import register_spread_symbols
        lot = self.state.get('lot', 'stop-monitor')
        register_spread_symbols(self.state, lot, log)

        if self._broker_actions_frozen():
            state_mod.save_state(self.json_path, self.state)
            return

        self._maybe_rearm_after_window_open()

        if self.state.get('close_only_mode'):
            self._recover_closing_on_load()
            state_mod.save_state(self.json_path, self.state)
            return

        active = self.state.get('active_stop')
        if active and active.get('order_id'):
            result = self.broker.get_order_status(
                active['order_id'],
                priority=PRIORITY_LOW,
                op=OPERATION_WORKING_STOP_RECONCILE,
            )
            if result.success:
                st = str(result.status).lower()
                if st in ('cancelled', 'canceled', 'rejected', 'expired'):
                    self.state['active_stop'] = None
                    self.state['stop_quantity'] = 0
                else:
                    active['status'] = result.status
                    if stop_order_fully_filled(self.state, result):
                        self.handle_stop_order_update(active, broker_result=result)
                self._maybe_merge_disk_stop_state()
                state_mod.save_state(self.json_path, self.state)
        self._ensure_stop_for_filled_qty()
        if self.state.get('status') == 'open' and not stop_is_current(
            self.state,
            ownership_conflict=has_ownership_conflict(self.state),
        ):
            state_mod.save_state(self.json_path, self.state)

        self._recover_closing_on_load()

    def _recover_closing_on_load(self) -> None:
        """G8: resume long close after stop_monitor restart."""
        if self._broker_actions_blocked():
            return
        if self.state.get('status') != 'closing':
            return

        if self.state.get('spread_close_order_id'):
            self._poll_spread_close()
            return

        oid = self.state.get('long_close_order_id')
        if oid:
            result = self.broker.get_order_status(
                str(oid),
                priority=PRIORITY_HIGH,
                op=OPERATION_LONG_CLOSE_STATUS,
            )
            if result.success and str(result.status).lower() == 'filled':
                self.state['long_close_price'] = result.filled_price
                self._finalize_close(reason=self.state.get('close_mechanism') or 'stop_filled')
                return

        close_started = self.state.get('short_closed_at')
        if close_started is None:
            self.state['short_closed_at'] = time.time() - self.long_close_delay_sec
            state_mod.save_state(self.json_path, self.state)
            log.warning(
                'closing trade missing short_closed_at — treating long close as due (%s)',
                self.json_path,
            )
            return

        if time.time() - float(close_started) >= self.long_close_delay_sec and not self._long_chase_active:
            log.info('Resuming long close chase on load for %s', self.json_path)
            self._long_chase_active = True
            t = threading.Thread(
                target=self._threaded_long_chase,
                name=f'long-chase-recover-{self.state.get("lot", "?")}',
                daemon=True,
            )
            t.start()

    def _ensure_stop_for_filled_qty(self) -> None:
        """Place or resize exchange stop only for filled spread units (both legs)."""
        if self._broker_actions_blocked():
            return
        self._ensure_stop_for_filled_qty_unblocked()

    def _ensure_stop_for_filled_qty_unblocked(self) -> None:
        if self.state.get('broker_actions_disabled'):
            return
        if state_mod.section(self.state, 'active_stop').get('skip_initial'):
            return
        if self.state.get('structure_type') == 'butterfly':
            return
        if not self.phases:
            return

        if self.state.get('status') != 'open':
            return

        filled = stop_qty_for_state(self.state)
        if filled <= 0:
            return

        short_fill = float(self.state['short_leg'].get('fill_price') or 0)
        long_fill = float(self.state['long_leg'].get('fill_price') or 0)
        if short_fill <= 0 or long_fill <= 0:
            return

        if has_ownership_conflict(self.state):
            log.critical(
                'stop_ownership_conflict — skip auto stop place for %s (operator repair required)',
                self.json_path,
            )
            return

        active = self.state.get('active_stop') or {}
        strategy_name = self.state.get('strategy') or ''
        is_meic = (strategy_name == 'MEIC_IC' or getattr(self.stop_profile, 'name', '') in ('meic_credit_spread', 'meic_2x_short'))
        if is_meic and active.get('order_id') and str(active.get('status', '')).lower() in ('working', 'live', 'open'):
            from common.meic_settings import is_15c_oco_enabled
            trades_root = _trades_root_for_path(self.json_path)
            desired_oco = is_15c_oco_enabled(root=trades_root)
            current_is_oco = (active.get('type') == 'OCO')
            if current_is_oco != desired_oco:
                log.info(
                    'Stop type mismatch for %s (current: %s, desired OCO: %s) — replacing stop',
                    self.json_path,
                    active.get('type'),
                    desired_oco,
                )
                self._reconcile_active_stop_type(desired_oco)
                return

        if stop_is_current(self.state):
            return

        if time.time() < self._stop_place_backoff_until:
            return

        active = self.state.get('active_stop') or {}
        if active.get('order_id') and str(active.get('status', '')).lower() in (
            'cancelled', 'canceled', 'rejected', 'expired',
        ):
            log.warning(
                'Exchange stop %s is %s — placing replacement',
                active.get('order_id'),
                active.get('status'),
            )
            self._reregister_alert(active.get('order_id'), None)
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            state_mod.save_state(self.json_path, self.state)
            self.setup_initial_stop()
            return

        if active.get('order_id') and int(self.state.get('stop_quantity') or 0) != filled:
            self._resize_stop(filled)
            return

        if not active.get('order_id'):
            self.setup_initial_stop()

    def _reconcile_active_stop_type(self, desired_oco: bool) -> None:
        active = self.state.get('active_stop') or {}
        old_oid = active.get('order_id')
        if not old_oid:
            return
        short = self.state.get('short_leg') or {}
        short_sym = short.get('symbol')
        if not short_sym:
            return
        stop_price = float(active.get('stop_price') or 0.0)
        limit_price = float(active.get('limit_price') or 0.0)
        qty = int(active.get('quantity') or self.state.get('stop_quantity') or 1)
        phase = active.get('phase', 1)
        if stop_price <= 0:
            return

        self.broker.cancel_order(str(old_oid))
        if desired_oco:
            res = self.broker.place_oco_order(
                short_sym,
                qty,
                profit_price=0.15,
                stop_price=stop_price,
                limit_price=limit_price,
            )
            new_type = 'OCO'
            pt_price = 0.15
        else:
            res = self.broker.place_stop_order(
                short_sym,
                qty,
                stop_price=stop_price,
                limit_price=limit_price,
            )
            new_type = 'STOP_LIMIT'
            pt_price = None

        if res.success:
            new_oid = str(res.order_id)
            self.state['active_stop'] = {
                'order_id': new_oid,
                'type': new_type,
                'stop_price': stop_price,
                'limit_price': limit_price,
                'profit_price': pt_price,
                'phase': phase,
                'status': 'working',
                'placed_at': state_mod.now_iso(),
                'quantity': qty,
            }
            state_mod.append_stop_history(
                self.state,
                action='replaced',
                order_id=new_oid,
                price=stop_price,
                phase=phase,
                reason='switch_oco_flag_enabled' if desired_oco else 'switch_oco_flag_disabled',
            )
            state_mod.save_state(self.json_path, self.state)
            self._reregister_alert(old_oid, new_oid)
            log.info('Replaced stop on %s with %s (#%s)', self.json_path, new_type, new_oid)
        else:
            log.error('Failed to place replacement order for %s: %s', self.json_path, res.message)

    def _resize_stop(self, new_qty: int) -> None:
        active = self.state.get('active_stop') or {}
        old_oid = active.get('order_id')
        if old_oid:
            log.info('Resizing stop: cancel %s (%s) → %s contracts', old_oid, self.state.get('stop_quantity'), new_qty)
            cancel = self.broker.cancel_order(str(old_oid))
            if not cancel.success and cancel.status not in ('filled', 'cancelled', 'canceled'):
                log.error(
                    'Stop resize: could not cancel %s (%s) — not placing replacement',
                    old_oid,
                    cancel.message,
                )
                return
        self._reregister_alert(old_oid, None)
        self.state['active_stop'] = None
        self.state['stop_quantity'] = 0
        state_mod.save_state(self.json_path, self.state)
        self.setup_initial_stop()

    def _sync_entry_fills(self) -> None:
        """Deprecated — entry monitor owns open-order fills before stop handoff."""
        return

    def _apply_spread_close_fill(self, result: OrderResult) -> None:
        self.state['short_close_price'] = (
            result.short_fill_price or result.filled_price
        )
        self.state['long_close_price'] = result.long_fill_price
        self.state['spread_close_order_id'] = None
        self.state['active_stop'] = None
        self._finalize_close(reason=self.state.get('close_mechanism') or 'manual_close')

    def _poll_spread_close(self) -> bool:
        """Poll working spread-close order. True = handled; skip leg-by-leg long chase."""
        oid = self.state.get('spread_close_order_id')
        if not oid:
            return False
        result = self.broker.get_order_status(
            str(oid),
            priority=PRIORITY_HIGH,
            op=OPERATION_SPREAD_CLOSE_STATUS,
        )
        if not result.success:
            return True
        status = str(result.status).lower()
        if status == 'filled':
            self._apply_spread_close_fill(result)
            return True
        if status in ('cancelled', 'canceled', 'rejected', 'expired'):
            log.warning('Spread close %s %s — clearing id', oid, status)
            self.state['spread_close_order_id'] = None
            state_mod.save_state(self.json_path, self.state)
            mechanism = self.state.get('close_mechanism') or ''
            if mechanism in ('manual_close', 'admin_killswitch'):
                self.replace_with_spread_close(reason=mechanism)
            return True
        return True

    def replace_with_spread_close(
        self,
        reason: str = 'manual_close',
        debit_limit: float | None = None,
    ) -> None:
        """Kill path — close both legs in one spread order (not leg-by-leg)."""
        if self._broker_actions_blocked():
            return
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if oid:
            outcome = self._cancel_stop_and_confirm(oid)
            if outcome == 'filled':
                result = self.broker.get_order_status(
                    str(oid),
                    priority=PRIORITY_HIGH,
                    op=OPERATION_SPREAD_CLOSE_STATUS,
                )
                self.handle_stop_order_update(active, broker_result=result)
                return
            if outcome != 'cancelled':
                log.error(
                    'Spread close: stop %s not cancelled at broker — aborting',
                    oid,
                )
                return
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            state_mod.append_stop_history(
                self.state,
                action='cancelled',
                order_id=oid,
                price=active.get('stop_price') or active.get('limit_price'),
                phase=active.get('phase', 1),
                reason=f'spread_close_cancel:{reason}',
                spx_price_at_event=self.prices.get_spx(),
            )

        short_sym = self.state['short_leg']['symbol']
        long_sym = self.state['long_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        if debit_limit is not None:
            debit = round_spx_option_price(float(debit_limit))
        else:
            short_p = self.prices.get_market_mid(short_sym) or self.prices.get(short_sym)
            long_p = self.prices.get_market_mid(long_sym) or self.prices.get(long_sym)
            if short_p is None or long_p is None:
                log.error('Spread close: missing MQTT quotes for %s / %s', short_sym, long_sym)
                return
            raw_debit = max(float(short_p) - float(long_p), 0.05)
            debit = round_spx_option_price(raw_debit + app_config.OPEN_PRICE_ADJ)
        result = self.broker.place_spread_close_order(short_sym, long_sym, qty, debit)
        if not result.success:
            if is_instruments_stopped_trading_error(result.message):
                self._mark_instruments_stopped_trading(result.message)
            else:
                log.error('Spread close order failed: %s', result.message)
            return

        if not self.state.get('close_mechanism'):
            self.state['close_mechanism'] = reason

        if str(result.status).lower() == 'filled':
            self._apply_spread_close_fill(result)
            log.info('Spread close filled immediately order=%s', result.order_id)
            return

        self.state['spread_close_order_id'] = result.order_id
        self.state['status'] = 'closing'
        state_mod.save_state(self.json_path, self.state)
        log.info('Spread close working order=%s debit=%.2f qty=%s', result.order_id, debit, qty)

    def _check_stop_update_command(self) -> bool:
        if self.state.get('status') != 'open':
            return False

        filename = os.path.basename(self.json_path)
        trades_root = _trades_root_for_path(self.json_path)
        cmd_path = os.path.join(trades_root, 'commands', f'{filename}.stop_update.json')
        if not os.path.exists(cmd_path):
            return False

        try:
            with open(cmd_path, 'r', encoding='utf-8') as f:
                cmd = json.load(f)
        except Exception:
            cmd = {}

        if 'profit_target_pct' in cmd:
            pt = float(cmd['profit_target_pct'])
            self.state['profit_target_pct'] = pt
            if 'exit_config' not in self.state or not isinstance(self.state['exit_config'], dict):
                self.state['exit_config'] = {}
            self.state['exit_config']['profit_target_pct'] = pt
            if isinstance(self.state.get('plan'), dict):
                self.state['plan']['profit_target_pct'] = pt

        if 'stop_multiplier' not in cmd:
            state_mod.save_state(self.json_path, self.state)
            try:
                os.unlink(cmd_path)
            except Exception:
                pass
            log.info('Profit target update applied pt=%s for %s', self.state.get('profit_target_pct'), self.json_path)
            return True

        mult = float(cmd.get('stop_multiplier', 2))
        self.state['stop_multiplier'] = mult
        if isinstance(self.state.get('plan'), dict):
            self.state['plan']['stop_multiplier'] = mult
        apply_two_x_thresholds(self.state, mult)

        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if oid:
            outcome = self._cancel_stop_and_confirm(oid)
            if outcome == 'filled':
                result = self.broker.get_order_status(
                    str(oid),
                    priority=PRIORITY_HIGH,
                    op=OPERATION_SPREAD_CLOSE_STATUS,
                )
                self.handle_stop_order_update(active, broker_result=result)
                try:
                    os.unlink(cmd_path)
                except Exception:
                    pass
                return True
            if outcome != 'cancelled':
                log.error(
                    'Stop update: stop %s not cancelled (%s) — keeping command for retry',
                    oid,
                    outcome,
                )
                return False
            state_mod.append_stop_history(
                self.state,
                action='cancelled',
                order_id=oid,
                price=active.get('stop_price') or active.get('limit_price'),
                phase=active.get('phase', 1),
                reason='stop_update',
            )
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0

        stop_price, limit_price = exchange_stop_limit_prices(
            float(self.state['short_leg']['fill_price']),
            mult,
        )
        placed = False
        for attempt in range(5):
            if attempt > 0:
                time.sleep(2.0)
            if self._place_short_stop(stop_price, limit_price, phase=1, reason='stop_update'):
                placed = True
                break
        if not placed:
            log.error('Stop update place failed — command retained for retry')
            state_mod.save_state(self.json_path, self.state)
            return False
        self.state['designated_stop_price'] = stop_price
        state_mod.save_state(self.json_path, self.state)
        try:
            os.unlink(cmd_path)
        except Exception:
            pass
        log.info('Stop update applied mult=%sx stop=%.2f', mult, stop_price)
        return True

    def _check_dashboard_commands(self) -> bool:
        """Check for dashboard sentinel / command files. Returns True if an action was taken."""
        if self.state.get('status') != 'open':
            return False

        trades_dir = _trades_dir()
        trades_root = _trades_root_for_path(self.json_path)

        # 1. Kill switch — force-close all active trades (global sentinel)
        ks_path = os.path.join(trades_dir, 'killswitch.json')
        if os.path.exists(ks_path):
            log.info('Dashboard killswitch detected — spread-closing %s', self.json_path)
            self.state['close_mechanism'] = 'admin_killswitch'
            self.replace_with_spread_close(reason='admin_killswitch')
            return True

        # 2. Per-trade close command
        filename = os.path.basename(self.json_path)
        cmd_path = os.path.join(trades_root, 'commands', f'{filename}.close.json')
        if os.path.exists(cmd_path):
            log.info('Dashboard close command detected for %s', filename)
            try:
                with open(cmd_path, 'r', encoding='utf-8') as f:
                    cmd = json.load(f)
                mechanism = cmd.get('close_mechanism', 'manual_close')
            except Exception:
                mechanism = 'manual_close'
            self.state['close_mechanism'] = mechanism
            if mechanism in ('manual_close', 'admin_killswitch'):
                self.replace_with_spread_close(reason=mechanism)
            else:
                self.replace_with_limit_close(reason=mechanism)
            try:
                os.unlink(cmd_path)
            except Exception:
                pass
            return True

        return False

    def _check_scale_out_command(self) -> bool:
        """Check for dashboard scale-out command file. Returns True if an action was taken."""
        if self.state.get('status') != 'open':
            return False

        trades_root = _trades_root_for_path(self.json_path)
        filename = os.path.basename(self.json_path)
        cmd_path = os.path.join(trades_root, 'commands', f'{filename}.scale_out.json')
        done_path = os.path.join(trades_root, 'commands', f'{filename}.scale_out.done.json')

        if not os.path.exists(cmd_path):
            return False

        log.info('Dashboard scale-out command detected for %s', filename)
        cmd = {}
        try:
            with open(cmd_path, 'r', encoding='utf-8') as f:
                cmd = json.load(f)
        except Exception as exc:
            log.error('Failed to read scale_out command for %s: %s', filename, exc)
            return False

        close_qty = int(cmd.get('close_qty', 1))
        target_debit = cmd.get('target_debit')
        stop_loss = cmd.get('stop_loss') or cmd.get('stop_debit')

        from common.scale_out import execute_scale_out_in_monitor
        res = execute_scale_out_in_monitor(self, close_qty, target_debit, stop_loss=stop_loss)

        try:
            with open(done_path, 'w', encoding='utf-8') as f:
                json.dump(res, f, indent=2)
        except Exception as exc:
            log.warning('Failed to write scale_out done sentinel for %s: %s', filename, exc)

        try:
            os.unlink(cmd_path)
        except Exception:
            pass

        return True

    def _poll_once(self) -> None:
        """Fast path: breach detection + long close chase. Runs every ~3s."""
        self._maybe_merge_disk_stop_state()

        if self._broker_actions_frozen():
            state_mod.save_state(self.json_path, self.state)
            return

        self._maybe_rearm_after_window_open()

        now = time.time()

        # Check dashboard kill switch or per-trade close commands first
        if not self._skip_dashboard_commands and self._check_dashboard_commands():
            return

        if not self._skip_dashboard_commands and self._check_scale_out_command():
            return

        if self._check_stop_update_command():
            return

        if self._poll_spread_close():
            return

        if self.state.get('close_only_mode'):
            if self.state.get('status') == 'closing':
                close_started = self.state.get('short_closed_at')
                if close_started is not None and now - float(close_started) >= self.long_close_delay_sec and not self._long_chase_active:
                    self._long_chase_active = True
                    t = threading.Thread(
                        target=self._threaded_long_chase,
                        name=f'long-chase-{self.state.get("lot","?")}',
                        daemon=True,
                    )
                    t.start()
            self._refresh_breach_watch_display_only()
            self._maybe_merge_disk_stop_state()
            state_mod.save_state(self.json_path, self.state)
            return

        if self.state.get('status') == 'open':
            if not stop_is_current(
                self.state,
                ownership_conflict=has_ownership_conflict(self.state),
            ):
                self._ensure_stop_for_filled_qty()

        # Handle 'closing' status — chase the long close order (in background thread)
        if self.state.get('status') == 'closing':
            if self._poll_spread_close():
                return
            close_started = self.state.get('short_closed_at')
            if close_started is None:
                return
            if now - float(close_started) >= self.long_close_delay_sec and not self._long_chase_active:
                self._long_chase_active = True
                t = threading.Thread(
                    target=self._threaded_long_chase,
                    name=f'long-chase-{self.state.get("lot","?")}',
                    daemon=True,
                )
                t.start()
            return

        if self.state.get('status') != 'open':
            return

        streamer_stale = self._streamer_prices_stale()
        mqtt_cache_stale = mqtt_cache_is_stale(self.prices)
        self._refresh_breach_watch(streamer_stale, mqtt_cache_stale=mqtt_cache_stale)

        if mqtt_cache_stale or streamer_stale:
            self._maybe_merge_disk_stop_state()
            state_mod.save_state(self.json_path, self.state)
            return

        # Slow path: adaptive working-stop REST reconcile
        mqtt_healthy = not (mqtt_cache_stale or streamer_stale)
        if is_working_stop_reconcile_due(
            self.state,
            now,
            mqtt_healthy=mqtt_healthy,
            status=str(self.state.get('status') or 'open'),
            close_only_mode=bool(self.state.get('close_only_mode')),
        ):
            from common.broker_cooldown import should_skip_priority

            if not should_skip_priority(PRIORITY_LOW):
                snapshot = getattr(self, '_peaceful_live_orders', None)
                self._maybe_merge_disk_stop_state()
                self._reconcile_active_stop_with_broker(live_orders=snapshot)
                self._sync_working_stop_order()
                # Consume one-shot snapshot so we don't reuse a stale list forever
                self._peaceful_live_orders = None
            schedule_next_working_stop_reconcile(
                self.state,
                now,
                mqtt_healthy=mqtt_healthy,
                status=str(self.state.get('status') or 'open'),
                close_only_mode=bool(self.state.get('close_only_mode')),
            )
            self._last_broker_sync = now

        self.kill_switch = self.prices.kill_switch

        # Phase checks (breach detection is fast — MQTT only, microseconds).
        # If a phase triggers and requires broker API calls (breach response),
        # run the execution in a background thread so the main loop can continue
        # checking other spreads without blocking.
        if self._breach_active:
            return

        for phase in self.phases:
            if phase.should_activate(self):
                self._breach_active = True
                t = threading.Thread(
                    target=self._threaded_phase_execute,
                    args=(phase,),
                    name=f'breach-{self.state.get("lot","?")}-{self.state.get("entry",{}).get("side","?")}',
                    daemon=True,
                )
                t.start()
                return

        self._maybe_merge_disk_stop_state()
        state_mod.save_state(self.json_path, self.state)

    def _streamer_prices_stale(self) -> bool:
        """G6: skip breach decisions when MQTT streamer health is stale."""
        if _integration_mode():
            return False
        if self._0dte_past_market_close():
            return True
        now_ct = central_time()
        if now_ct.hour < 8 or (now_ct.hour >= 15 and now_ct.minute >= 0):
            return False
        if not streamer_prices_stale():
            self._stale_warned = False
            return False
        if not self._stale_warned:
            log.critical(
                'Streamer prices stale (>30s) — freezing breach checks for %s',
                self.json_path,
            )
            self._stale_warned = True
        return True

    def _threaded_phase_execute(self, phase: PhaseBase) -> None:
        """Run phase execution (breach response) in a background thread.

        This allows the MonitorRunner's main loop to continue scanning other
        spreads while this spread's broker API calls (cancel stop, place limit,
        reprice) are in progress.  Critical when multiple spreads breach
        simultaneously in a trending market.
        """
        try:
            phase.execute(self)
            with self._breach_lock:
                state_mod.save_state(self.json_path, self.state)
        except Exception:
            log.exception('Breach handler failed for %s', self.json_path)
        finally:
            self._breach_active = False

    def _threaded_long_chase(self) -> None:
        """Run the long close chase/replace cycle in a background thread."""
        try:
            self._chase_long_close()
            with self._breach_lock:
                state_mod.save_state(self.json_path, self.state)
        except Exception:
            log.exception('Long chase failed for %s', self.json_path)
        finally:
            self._long_chase_active = False

    def current_stop_price(self) -> float:
        return spread_breach_threshold(self.state)

    def _mqtt_cache_stale(self) -> bool:
        from common.mqtt_prices import mqtt_cache_is_stale
        return mqtt_cache_is_stale(self.prices)

    def _refresh_breach_watch_display_only(self) -> None:
        """Refresh breach_watch snapshot without evaluating breach transitions."""
        streamer_stale = self._streamer_prices_stale()
        mqtt_cache_stale = self._mqtt_cache_stale()
        self._refresh_breach_watch(streamer_stale, mqtt_cache_stale=mqtt_cache_stale, evaluate_transitions=False)

    def _refresh_breach_watch(
        self,
        streamer_stale: bool,
        *,
        mqtt_cache_stale: bool = False,
        evaluate_transitions: bool = True,
    ) -> None:
        """Persist spread vs software breach threshold; rate-limited diagnostics."""
        from blocks.stop.breach import spread_breach_triggered
        from blocks.stop.breach_quote import (
            evaluate_quote_pair_readiness,
            readiness_to_watch_fields,
            reset_breach_confirmation,
            update_breach_confirmation,
        )
        from blocks.stop.breach_config import BREACH_CONFIRM_OBSERVATIONS
        from blocks.stop.breach_watch import build_breach_watch_snapshot, log_breach_watch
        from blocks.stop.fill_reference import ensure_fill_reference_epoch
        from blocks.stop.stop_math import spread_breach_threshold

        ensure_fill_reference_epoch(self.state)
        short_sym = self.state['short_leg']['symbol']
        long_sym = self.state['long_leg']['symbol']
        if mqtt_cache_stale:
            short_p = None
            long_p = None
        else:
            short_p = self.prices.get_market_mid(short_sym)
            long_p = self.prices.get_market_mid(long_sym)

        readiness = evaluate_quote_pair_readiness(
            self.state,
            self.prices,
            streamer_stale=streamer_stale,
            mqtt_cache_stale=mqtt_cache_stale,
        )
        if evaluate_transitions and readiness.software_breach_ready and readiness.spread_mid is not None:
            threshold = spread_breach_threshold(self.state)
            spread_breached = spread_breach_triggered(readiness.spread_mid, threshold)
            confirmation = update_breach_confirmation(
                self.state,
                readiness,
                spread_breached=spread_breached,
            )
        else:
            if evaluate_transitions:
                reset_breach_confirmation(self.state)
            confirmation = {
                'breach_confirmation_count': 0,
                'breach_confirmation_required': BREACH_CONFIRM_OBSERVATIONS,
                'breach_confirmation_reason': readiness.quote_pair_reason,
                'software_breach_confirmed': False,
            }

        readiness_fields = readiness_to_watch_fields(readiness, confirmation)
        watch = build_breach_watch_snapshot(
            self.state,
            short_p=short_p,
            long_p=long_p,
            streamer_stale=streamer_stale,
            mqtt_cache_stale=mqtt_cache_stale,
            now_iso=state_mod.now_iso(),
            readiness=readiness_fields,
        )
        self.state['breach_watch'] = watch
        log_breach_watch(self, watch)

    def setup_initial_stop(self, stop_multiplier: Optional[float] = None) -> None:
        qty = stop_qty_for_state(self.state)
        if qty <= 0:
            return
        short = self.state['short_leg']
        mult = (
            float(stop_multiplier)
            if stop_multiplier is not None
            else stop_multiplier_for_state(self.state)
        )
        stop_price, limit_price = exchange_stop_limit_prices(short['fill_price'], mult)
        reason = f'initial_short_stop_{mult:g}x'
        if self._place_short_stop(stop_price, limit_price, phase=1, reason=reason, qty=qty):
            self.state['designated_stop_price'] = stop_price
            state_mod.save_state(self.json_path, self.state)
            new_oid = (self.state.get('active_stop') or {}).get('order_id')
            self._reregister_alert(None, new_oid)

    def setup_stop_at_price(
        self,
        stop_price: float,
        limit_price: Optional[float] = None,
        phase: int = 1,
        reason: str = 'manual_stop',
    ) -> bool:
        """Place a stop on the short leg at an explicit price (adhoc / manual)."""
        if limit_price is None:
            limit_price = round_spx_option_price(stop_price + app_config.LIMIT_OFFSET)
        return self._place_short_stop(stop_price, limit_price, phase=phase, reason=reason)

    def _adopt_working_closing_order_from_broker(
        self,
        short_sym: str,
        expected_qty: Optional[int] = None,
    ) -> bool:
        """If broker rejects placing a stop because a closing order is already working, adopt it."""
        try:
            found = None
            if hasattr(self.broker, 'find_working_close_order'):
                found = self.broker.find_working_close_order(short_sym)

            if not found or not getattr(found, 'order_id', None):
                if hasattr(self.broker, 'get_live_orders_cached'):
                    self.broker.get_live_orders_cached(ttl_sec=0)
                    if hasattr(self.broker, 'find_working_close_order'):
                        found = self.broker.find_working_close_order(short_sym)

            if found and getattr(found, 'order_id', None):
                adopted_oid = str(found.order_id)
                raw_order = getattr(found, 'raw', None)
                order_qty = getattr(found, 'order_quantity', None)
                if not order_qty and raw_order:
                    order_qty = getattr(raw_order, 'size', None)
                if not order_qty:
                    order_qty = expected_qty or stop_qty_for_state(self.state)

                stop_price = None
                limit_price = getattr(found, 'order_limit_price', None)
                profit_price = None
                if raw_order:
                    stop_price = getattr(raw_order, 'stop_trigger', None)
                    if stop_price is not None:
                        stop_price = float(stop_price)
                    if limit_price is None and getattr(raw_order, 'price', None) is not None:
                        limit_price = float(raw_order.price)

                order_type = 'STOP_LIMIT' if stop_price is not None else 'LIMIT'
                if raw_order and (getattr(raw_order, 'complex_order_id', None) or getattr(raw_order, 'order_type', '') == 'OCO'):
                    order_type = 'OCO'

                self.state['active_stop'] = {
                    'order_id': adopted_oid,
                    'type': order_type,
                    'stop_price': stop_price or self.state.get('designated_stop_price'),
                    'limit_price': limit_price,
                    'profit_price': profit_price,
                    'phase': 1,
                    'status': 'working',
                    'placed_at': state_mod.now_iso(),
                    'quantity': int(order_qty),
                }
                self.state['stop_quantity'] = int(order_qty)
                if order_qty and int(order_qty) != int(self.state.get('quantity') or 0):
                    self.state['quantity'] = int(order_qty)
                    self.state['filled_quantity'] = int(order_qty)

                state_mod.append_stop_history(
                    self.state,
                    action='adopted_from_broker',
                    order_id=adopted_oid,
                    price=stop_price or limit_price,
                    phase=1,
                    reason='adopted_existing_working_closing_order',
                    spx_price_at_event=self.prices.get_spx() if hasattr(self.prices, 'get_spx') else None,
                )
                self._reregister_alert(None, adopted_oid)
                state_mod.save_state(self.json_path, self.state)
                log.info(
                    'Adopted working closing order %s (qty=%s) from broker for %s',
                    adopted_oid,
                    order_qty,
                    self.json_path,
                )
                return True
        except Exception as exc:
            log.warning('Failed to adopt working closing order for %s: %s', short_sym, exc)
        return False

    def _place_short_stop(
        self,
        stop_price: float,
        limit_price: float,
        phase: int,
        reason: str,
        qty: Optional[int] = None,
    ) -> bool:
        if self._broker_actions_frozen():
            return False
        short = self.state['short_leg']
        short_sym = short.get('symbol')
        if not short_sym:
            return False
        try:
            held_qty = self.broker.get_single_leg_position_qty(short_sym)
            if held_qty >= 0:
                log.warning(
                    'Cannot place short stop/OCO: position for %s is already flat/long (qty=%d) — auto-finalizing short leg',
                    short_sym,
                    held_qty,
                )
                if not self.state.get('short_closed_at'):
                    self.state['short_closed_at'] = time.time()
                if not self.state.get('close_mechanism'):
                    self.state['close_mechanism'] = 'broker_position_flat'
                self.state['status'] = 'closing'
                self.state['active_stop'] = None
                self.state['stop_quantity'] = 0
                state_mod.save_state(self.json_path, self.state)
                return False
        except Exception as exc:
            log.warning('Broker position verification check failed in _place_short_stop: %s', exc)
            held_qty = None

        desired_qty = qty if qty is not None else stop_qty_for_state(self.state)
        qty = min(desired_qty, abs(held_qty)) if held_qty is not None else desired_qty
        if qty <= 0:
            return False
        stop_price = round_spx_option_price(stop_price)
        limit_price = round_spx_option_price(limit_price)

        strategy_name = self.state.get('strategy') or ''
        is_meic = (strategy_name == 'MEIC_IC' or getattr(self.stop_profile, 'name', '') in ('meic_credit_spread', 'meic_2x_short'))
        if is_meic:
            from common.meic_settings import is_15c_oco_enabled
            trades_root = _trades_root_for_path(self.json_path)
            use_oco = is_15c_oco_enabled(root=trades_root)
            pt_price = 0.15 if use_oco else None
        else:
            use_oco = (
                getattr(self.stop_profile, 'use_oco_short_stop', False)
                and getattr(self.stop_profile, 'profit_target_short', None) is not None
            )
            pt_price = float(self.stop_profile.profit_target_short) if use_oco else None

        if use_oco:
            try:
                result = self.broker.place_oco_order(
                    short['symbol'],
                    qty,
                    profit_price=pt_price,
                    stop_price=stop_price,
                    limit_price=limit_price,
                )
                if not isinstance(result, OrderResult) and hasattr(self.broker, 'place_stop_order'):
                    stop_res = self.broker.place_stop_order(
                        short['symbol'], qty, stop_price, limit_price
                    )
                    if isinstance(stop_res, OrderResult):
                        result = stop_res
            except (NotImplementedError, AttributeError):
                use_oco = False
                result = self.broker.place_stop_order(
                    short['symbol'], qty, stop_price, limit_price
                )
        else:
            result = self.broker.place_stop_order(
                short['symbol'], qty, stop_price, limit_price
            )

        if not result.success:
            log.error(
                'Stop order failed for %s qty=%s: %s',
                short['symbol'],
                qty,
                result.message,
            )
            msg = str(result.message or '').lower()
            if 'already have a closing order' in msg or 'cannot_close_against_more_than_existing_position' in msg:
                if self._adopt_working_closing_order_from_broker(short['symbol'], expected_qty=qty):
                    return True
            if is_instruments_stopped_trading_error(result.message):
                self._mark_instruments_stopped_trading(result.message)
            else:
                self._stop_place_backoff_until = time.time() + 60
            return False

        spx = self.prices.get_spx()
        oid = str(result.order_id) if getattr(result, 'order_id', None) is not None else None
        self.state['active_stop'] = {
            'order_id': oid,
            'type': 'OCO' if use_oco else 'STOP_LIMIT',
            'stop_price': stop_price,
            'limit_price': limit_price,
            'profit_price': pt_price,
            'phase': phase,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': qty,
        }
        self.state['stop_quantity'] = qty
        self.state['designated_stop_price'] = stop_price
        state_mod.append_stop_history(
            self.state,
            action='placed',
            order_id=oid,
            price=stop_price,
            phase=phase,
            reason=reason,
            spx_price_at_event=spx,
        )
        state_mod.save_state(self.json_path, self.state)
        log.info('Placed stop %s @ %s (%s)', oid, stop_price, reason)
        return True

    def adjust_stop_to_breakeven(self, reason: str = 'companion_breached') -> bool:
        """Tighten working stop/OCO to breakeven (1x / short fill price) when companion IC breaches."""
        if self._broker_actions_frozen():
            return False
        if self.state.get('status') != 'open':
            return False
        if self.state.get('phases', {}).get('breakeven_tightened'):
            return False

        short = self.state.get('short_leg') or {}
        short_sym = short.get('symbol')
        if not short_sym:
            return False
        try:
            held_qty = self.broker.get_single_leg_position_qty(short_sym)
            if held_qty >= 0:
                log.warning(
                    'Cannot adjust breakeven stop: position for %s is already flat/long (qty=%d) — auto-finalizing short leg',
                    short_sym,
                    held_qty,
                )
                if not self.state.get('short_closed_at'):
                    self.state['short_closed_at'] = time.time()
                if not self.state.get('close_mechanism'):
                    self.state['close_mechanism'] = 'broker_position_flat'
                self.state['status'] = 'closing'
                self.state['active_stop'] = None
                self.state['stop_quantity'] = 0
                state_mod.save_state(self.json_path, self.state)
                return False
        except Exception as exc:
            log.warning('Broker position verification check failed in adjust_stop_to_breakeven: %s', exc)
            held_qty = None

        if reason == 'companion_breached':
            trades_root = _trades_root_for_path(self.json_path)
            from common.meic_settings import is_companion_tightening_enabled
            if not is_companion_tightening_enabled(root=trades_root):
                log.info('Companion breach tightening disabled in settings; skipping breakeven adjustment for %s', self.json_path)
                return False

        short_fill = float(short.get('fill_price') or 0)
        if short_fill <= 0:
            return False

        be_stop, be_limit = exchange_stop_limit_prices(short_fill, multiplier=1.0)
        active = self.state.get('active_stop') or {}
        old_oid = active.get('order_id')
        desired_qty = stop_qty_for_state(self.state)
        qty = min(desired_qty, abs(held_qty)) if held_qty is not None else desired_qty
        if qty <= 0:
            return False

        strategy_name = self.state.get('strategy') or ''
        is_meic = (strategy_name == 'MEIC_IC' or getattr(self.stop_profile, 'name', '') in ('meic_credit_spread', 'meic_2x_short'))
        if is_meic:
            from common.meic_settings import is_15c_oco_enabled
            trades_root = _trades_root_for_path(self.json_path)
            use_oco = is_15c_oco_enabled(root=trades_root)
            pt_price = 0.15 if use_oco else None
        else:
            use_oco = (
                getattr(self.stop_profile, 'use_oco_short_stop', False)
                and getattr(self.stop_profile, 'profit_target_short', None) is not None
            )
            pt_price = float(self.stop_profile.profit_target_short) if use_oco else None

        if old_oid:
            self.broker.cancel_order(str(old_oid))

        if use_oco:
            try:
                result = self.broker.place_oco_order(
                    short['symbol'],
                    qty,
                    profit_price=pt_price,
                    stop_price=be_stop,
                    limit_price=be_limit,
                )
                if not isinstance(result, OrderResult) and hasattr(self.broker, 'place_stop_order'):
                    stop_res = self.broker.place_stop_order(
                        short['symbol'], qty, be_stop, be_limit
                    )
                    if isinstance(stop_res, OrderResult):
                        result = stop_res
            except (NotImplementedError, AttributeError):
                use_oco = False
                result = self.broker.place_stop_order(
                    short['symbol'], qty, be_stop, be_limit
                )
        else:
            result = self.broker.place_stop_order(
                short['symbol'], qty, be_stop, be_limit
            )

        if not result.success:
            log.error('Failed to place breakeven stop for %s: %s', short.get('symbol'), result.message)
            return False

        spx = self.prices.get_spx()
        oid = str(result.order_id) if getattr(result, 'order_id', None) is not None else None
        self.state['active_stop'] = {
            'order_id': oid,
            'type': 'OCO' if use_oco else 'STOP_LIMIT',
            'stop_price': be_stop,
            'limit_price': be_limit,
            'profit_price': pt_price,
            'phase': active.get('phase', 1),
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': qty,
        }
        self.state['stop_quantity'] = qty
        self.state['designated_stop_price'] = be_stop
        self.state['stop_multiplier'] = 1.0
        self.state.setdefault('phases', {})['breakeven_tightened'] = True
        self.state['phases']['breakeven_tightened_at'] = state_mod.now_iso()

        state_mod.append_stop_history(
            self.state,
            action='tightened_breakeven',
            order_id=oid,
            price=be_stop,
            phase=active.get('phase', 1),
            reason=reason,
            spx_price_at_event=spx,
        )
        state_mod.save_state(self.json_path, self.state)
        log.info(
            'Adjusted stop to breakeven for %s: stop=%.2f limit=%.2f order=%s (%s)',
            self.json_path,
            be_stop,
            be_limit,
            oid,
            reason,
        )
        return True

    def upgrade_to_spread_stop(self) -> None:
        active = self.state.get('active_stop') or {}
        old_oid = active.get('order_id')
        if old_oid:
            self.broker.cancel_order(old_oid)

        short = self.state['short_leg']
        qty = stop_qty_for_state(self.state)
        spread_stop = self.state['entry']['two_x_net_credit']
        stop_price = round_spx_option_price(spread_stop)
        limit_price = round_spx_option_price(spread_stop + app_config.LIMIT_OFFSET)

        result = self.broker.place_stop_order(
            short['symbol'], qty, stop_price, limit_price
        )
        spx = self.prices.get_spx()
        self.state['phases']['short_stoplmt_replaced'] = True
        self.state['phases']['phase2_activated_at'] = state_mod.now_iso()
        if not self.state.get('close_mechanism'):
            self.state['close_mechanism'] = 'phase2_upgrade'
        self.state['active_stop'] = {
            'order_id': result.order_id,
            'type': 'STOP_LIMIT',
            'stop_price': stop_price,
            'limit_price': limit_price,
            'phase': 2,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': qty,
        }
        self.state['stop_quantity'] = qty
        self.state['designated_stop_price'] = stop_price
        state_mod.append_stop_history(
            self.state,
            action='placed',
            order_id=result.order_id,
            price=stop_price,
            phase=2,
            reason='phase2_upgrade',
            spx_price_at_event=spx,
        )
        self._reregister_alert(old_oid, result.order_id)
        state_mod.save_state(self.json_path, self.state)

    def _sync_working_stop_order(self) -> None:
        """Poll broker for STOP_LIMIT / LIMIT / OCO fill (exchange stop, PT, or breach close)."""
        active = self.state.get('active_stop') or {}
        if active.get('order_id') and active.get('type') in ('STOP_LIMIT', 'LIMIT', 'OCO') and str(active.get('status', '')).lower() not in ('filled', 'cancelled', 'rejected'):
            self._sync_active_close_order(
                priority=PRIORITY_LOW,
                op=OPERATION_WORKING_STOP_RECONCILE,
            )
        self._sync_working_scale_out_orders()

    def _sync_working_scale_out_orders(self, live_orders=None) -> None:
        """Poll broker for status of working scale-out orders (including OCO)."""
        scale_outs = self.state.get('scale_outs') or []
        changed = False
        net_credit = float(self.state.get('entry', {}).get('net_credit', 0) or 0)
        for s in scale_outs:
            st = str(s.get('status') or '').lower()
            oid = str(s.get('order_id') or '')
            if oid and st in ('working', 'live', 'contingent', 'received', 'open'):
                res = self.broker.get_order_status(
                    oid,
                    priority=PRIORITY_LOW,
                    op='scale_out_status',
                    live_orders=live_orders,
                )
                if not res.success:
                    continue
                new_st = str(res.status).lower()
                if new_st == 'filled':
                    s['status'] = 'filled'
                    fill_p = float(res.filled_price) if res.filled_price is not None else float(s.get('debit_limit') or 0)
                    s['close_price'] = fill_p
                    qty = int(s.get('quantity') or 1)
                    s['pnl'] = round((net_credit - fill_p) * 100 * qty, 2)
                    changed = True
                    log.info('Scale-out order %s filled @ %.2f (P&L $%.2f)', oid, fill_p, s['pnl'])
                elif new_st in ('cancelled', 'canceled', 'rejected'):
                    s['status'] = 'cancelled'
                    changed = True
                    log.info('Scale-out order %s marked terminal (%s) at broker', oid, new_st)
        if changed:
            state_mod.save_state(self.json_path, self.state)

    def _reconcile_active_stop_with_broker(self, live_orders=None) -> None:
        """Refresh this JSON's own active_stop at broker — never adopt by short symbol.

        When ``live_orders`` is provided (batched peaceful cycle), search that
        snapshot first; direct get_order only if the id is absent.
        """
        if self.state.get('status') != 'open':
            return

        self._sync_working_scale_out_orders(live_orders=live_orders)

        active = self.state.get('active_stop') or {}
        oid = str(active.get('order_id') or '')
        if not oid:
            return

        result = self.broker.get_order_status(
            oid,
            priority=PRIORITY_LOW,
            op=OPERATION_WORKING_STOP_RECONCILE,
            live_orders=live_orders,
        )
        if not result.success:
            return

        st = str(result.status).lower()
        if stop_order_fully_filled(self.state, result):
            self.handle_stop_order_update(active, broker_result=result)
            return

        is_partial = (
            st in ('partial', 'partially filled', 'partially_filled')
            or (
                result.filled_quantity is not None
                and result.filled_quantity > 0
                and not stop_order_fully_filled(self.state, result)
            )
        )
        if is_partial:
            self.handle_partial_stop_fill(active, broker_result=result)
            return

        if st in ('working', 'live', 'contingent', 'received', 'open'):
            active['status'] = 'working'
            state_mod.save_state(self.json_path, self.state)
            return

        if st in ('cancelled', 'canceled', 'rejected', 'expired', 'unknown'):
            if result.filled_quantity and int(result.filled_quantity) > 0:
                self.handle_partial_stop_fill(active, broker_result=result)
                return
            # First check if disk state has already moved on to a new working stop (e.g. from scale_out)
            self._maybe_merge_disk_stop_state()
            current_active = self.state.get('active_stop') or {}
            current_oid = str(current_active.get('order_id') or '')
            if current_oid and current_oid != oid and str(current_active.get('status', '')).lower() in (
                'working', 'live', 'contingent', 'received', 'open',
            ):
                log.info(
                    'Stop %s was cancelled, but trade state already has new stop %s — skipping clear',
                    oid, current_oid,
                )
                return

            state_mod.append_stop_history(
                self.state,
                action='cancelled',
                order_id=oid,
                price=active.get('stop_price') or active.get('limit_price'),
                phase=active.get('phase', 1),
                reason='own_stop_terminal_at_broker',
                spx_price_at_event=self.prices.get_spx(),
            )
            self._reregister_alert(oid, None)
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            state_mod.save_state(self.json_path, self.state)

    def _maybe_merge_disk_stop_state(self) -> None:
        """Adopt manual stop JSON edits when in-memory state is stale."""
        try:
            disk = state_mod.load_state(self.json_path)
        except Exception:
            return

        if disk.get('status') == 'closed' and self.state.get('status') != 'closed':
            self.state['status'] = 'closed'
            self.state['close_mechanism'] = disk.get('close_mechanism', 'manual_close')
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            log.info('Adopted closed status from disk for %s', self.json_path)
            return

        disk_filled = disk.get('filled_quantity')
        if disk_filled is not None and disk_filled != self.state.get('filled_quantity'):
            self.state['filled_quantity'] = int(disk_filled)
            if disk.get('quantity') is not None:
                self.state['quantity'] = int(disk['quantity'])
            if disk.get('open_order'):
                self.state['open_order'] = disk['open_order']
            log.info('Adopted disk fill count for %s → %s filled', self.json_path, disk_filled)

        if disk.get('scale_outs') is not None:
            self.state['scale_outs'] = disk['scale_outs']
        if disk.get('initial_quantity') is not None:
            self.state['initial_quantity'] = disk['initial_quantity']

        disk_stop = disk.get('active_stop') or {}
        if not disk_stop.get('order_id'):
            return

        mem_stop = self.state.get('active_stop') or {}
        disk_oid = str(disk_stop['order_id'])
        mem_oid = str(mem_stop.get('order_id') or '')
        disk_st = str(disk_stop.get('status', '')).lower()
        mem_st = str(mem_stop.get('status', '')).lower()
        working = ('working', 'live', 'contingent', 'received', 'open')

        adopt = False
        if disk_st in working and mem_st in ('cancelled', 'canceled', 'rejected', ''):
            adopt = True
        elif disk_oid != mem_oid and disk_st in working:
            adopt = True

        if not adopt:
            return

        old_oid = mem_oid or None
        self.state['active_stop'] = dict(disk_stop)
        if disk.get('stop_quantity') is not None:
            self.state['stop_quantity'] = disk['stop_quantity']
        if disk.get('designated_stop_price') is not None:
            self.state['designated_stop_price'] = disk['designated_stop_price']
        if disk.get('stop_history'):
            self.state['stop_history'] = disk['stop_history']
        if disk_oid != mem_oid:
            self._reregister_alert(old_oid, disk_oid)
            log.info('Adopted disk stop state for %s → order %s', self.json_path, disk_oid)

    def _sync_active_close_order(
        self,
        *,
        priority: str = PRIORITY_HIGH,
        op: str = OPERATION_SPREAD_CLOSE_STATUS,
    ) -> None:
        """Poll broker for fill/cancel on the working short-leg close order."""
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if not oid:
            return
        result = self.broker.get_order_status(
            str(oid),
            priority=priority,
            op=op,
        )
        if not result.success:
            return
        active['status'] = result.status
        if stop_order_fully_filled(self.state, result):
            self.handle_stop_order_update(active, broker_result=result)
        elif result.filled_quantity and int(result.filled_quantity) > 0:
            self.handle_partial_stop_fill(active, broker_result=result)
        state_mod.save_state(self.json_path, self.state)

    def breach_short_limit_target(self, short_symbol: str) -> Optional[float]:
        """Live MQTT short mid rounded to a valid SPX tick (breach limit price)."""
        short_p = self.prices.get_market_mid(short_symbol)
        if short_p is None:
            short_p = self.prices.get(short_symbol)
        if short_p is None:
            return None
        return round_spx_option_price(short_p)

    def needs_breach_limit_reprice(self, active: dict, target: float) -> bool:
        """True when streamed mid implies a different limit than the working order."""
        current = active.get('limit_price')
        if current is None:
            return True
        return round_spx_option_price(float(current)) != target

    def _cancel_stop_and_confirm(self, order_id: str, timeout: float = 30.0) -> str:
        """
        Cancel exchange stop and poll broker until it leaves the live book.
        Returns 'cancelled', 'filled', or 'failed'.
        """
        oid = str(order_id)
        status = self.broker.get_order_status(
            oid,
            priority=PRIORITY_HIGH,
            op=OPERATION_STOP_CANCEL,
        )
        if status.success and str(status.status).lower() == 'filled':
            return 'filled'

        cancel = self.broker.cancel_order(oid)
        if not cancel.success and str(cancel.status).lower() == 'filled':
            return 'filled'

        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.broker.get_order_status(
                oid,
                priority=PRIORITY_HIGH,
                op=OPERATION_STOP_CANCEL,
            )
            low = str(st.status).lower()
            if st.success:
                if low in ('cancelled', 'canceled'):
                    return 'cancelled'
                if low == 'filled':
                    return 'filled'
                if low in ('working', 'live', 'received', 'open', 'contingent', 'partially filled'):
                    time.sleep(0.5)
                    continue
            elif low == 'unknown':
                return 'cancelled'
            time.sleep(0.5)

        log.error('Stop %s cancel not confirmed within %.0fs', oid, timeout)
        return 'failed'

    def replace_with_limit_close(self, reason: str = 'spread_stop_breach') -> None:
        if self._broker_actions_blocked():
            return
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        phase = active.get('phase', 1)

        if not oid:
            log.error('Breach limit close: no active stop in JSON or broker — JSON unchanged')
            return

        outcome = self._cancel_stop_and_confirm(oid)
        if outcome == 'filled':
            result = self.broker.get_order_status(
                str(oid),
                priority=PRIORITY_HIGH,
                op=OPERATION_SPREAD_CLOSE_STATUS,
            )
            self.handle_stop_order_update(active, broker_result=result)
            return
        if outcome != 'cancelled':
            log.error(
                'Breach limit close: stop %s not cancelled at broker — JSON unchanged',
                oid,
            )
            return

        status_after = self.broker.get_order_status(
            str(oid),
            priority=PRIORITY_HIGH,
            op=OPERATION_STOP_CANCEL,
        )
        if status_after.success and status_after.filled_quantity:
            partial_qty = int(status_after.filled_quantity)
            if partial_qty > 0:
                partial_px = float(status_after.filled_price or active.get('stop_price') or 0.0)
                partial_fills = self.state.setdefault('short_partial_fills', [])
                if not any(str(p.get('order_id')) == str(oid) for p in partial_fills):
                    partial_fills.append({
                        'order_id': str(oid),
                        'qty': partial_qty,
                        'price': partial_px,
                        'timestamp': state_mod.now_iso(),
                    })

        is_pt = 'profit_target' in reason
        cancel_reason = f'pt_cancel:{reason}' if is_pt else f'breach_cancel:{reason}'
        state_mod.append_stop_history(
            self.state,
            action='cancelled',
            order_id=oid,
            price=active.get('stop_price') or active.get('limit_price'),
            phase=phase,
            reason=cancel_reason,
            spx_price_at_event=self.prices.get_spx(),
        )

        short_sym = self.state['short_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        try:
            held = self.broker.get_option_position_quantity(short_sym)
            if held is not None:
                remaining_short = abs(held) if held < 0 else 0
                qty = min(qty, remaining_short)
        except Exception as exc:
            log.warning('Could not get broker held position in replace_with_limit_close: %s', exc)

        if qty <= 0:
            log.info('Position for %s is already flat at broker — finalizing short leg', short_sym)
            self.state['short_close_price'] = self._calculate_blended_short_close_price()
            self.state['short_closed_at'] = time.time()
            self.state['status'] = 'closing'
            self.state['short_remaining_qty'] = 0
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            state_mod.save_state(self.json_path, self.state)
            return

        short_p = self.prices.get_market_mid(short_sym)
        if short_p is None:
            short_p = self.prices.get(short_sym)
        if short_p is None and active.get('stop_price'):
            short_p = float(active['stop_price'])
        if short_p is None:
            log.error('Breach limit close: no MQTT price for %s — stop cancelled, no limit placed', short_sym)
            return
        limit_price = round_spx_option_price(short_p)

        result = self.broker.place_limit_order('BUY_TO_CLOSE', short_sym, qty, limit_price)
        if not result.success:
            log.error('Breach limit close failed after cancel: %s', result.message)
            return

        old_oid = oid
        self.state['active_stop'] = {
            'order_id': result.order_id,
            'type': 'LIMIT',
            'stop_price': None,
            'limit_price': limit_price,
            'phase': phase,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': qty,
        }
        self.state['short_remaining_qty'] = qty
        self.state['stop_quantity'] = qty
        if is_pt:
            self.state['phases']['pt_limit_placed_at'] = state_mod.now_iso()
            self.state['close_mechanism'] = 'profit_target'
        else:
            self.state['phases']['breach_limit_placed_at'] = state_mod.now_iso()
            if not self.state.get('close_mechanism'):
                self.state['close_mechanism'] = 'software_breach'
        long_sym = self.state['long_leg']['symbol']
        self.prices.clear_override(short_sym)
        self.prices.clear_override(long_sym)
        self._reregister_alert(old_oid, result.order_id)
        state_mod.append_stop_history(
            self.state,
            action='replaced_limit',
            order_id=result.order_id,
            price=limit_price,
            phase=phase,
            reason=reason,
            spx_price_at_event=self.prices.get_spx(),
        )
        state_mod.save_state(self.json_path, self.state)
        log.info(
            'Breach: cancelled stop %s, placed limit %s @ %s on %s',
            oid,
            result.order_id,
            limit_price,
            short_sym,
        )

    def execute_spx_proximity_close(self) -> None:
        if self._broker_actions_blocked():
            return
        if self.state['phases'].get('phase3_activated_at') is None:
            self.state['phases']['phase3_activated_at'] = state_mod.now_iso()

        spx = self.prices.get_spx()
        if spx is None:
            return

        short_strike = self.state['short_leg']['strike']
        side = self.state['entry']['side']
        diff = app_config.STRK_IDX_DIFF

        triggered = False
        if side == 'C' and short_strike - spx <= diff:
            triggered = True
        elif side == 'P' and spx - short_strike <= diff:
            triggered = True

        if not triggered:
            return

        active = self.state.get('active_stop') or {}
        if active.get('order_id'):
            self.broker.cancel_order(active['order_id'])

        short_sym = self.state['short_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        result = self.broker.close_at_market(short_sym, qty)
        if result.success:
            state_mod.append_stop_history(
                self.state,
                action='market_close',
                order_id=result.order_id,
                price=None,
                phase=3,
                reason='phase3_spx_proximity',
                spx_price_at_event=spx,
            )
            self.state['close_mechanism'] = 'phase3_proximity'
            self.state['status'] = 'closing'
            self.state['short_closed_at'] = time.time()
            state_mod.save_state(self.json_path, self.state)
            log.info(
                'Phase 3 short closed — status=closing, long close in %ss',
                self.long_close_delay_sec,
            )

    def _calculate_blended_short_close_price(
        self,
        final_fill_price: Optional[float] = None,
        final_qty: Optional[int] = None,
    ) -> Optional[float]:
        partial_fills = self.state.get('short_partial_fills') or []
        if not partial_fills:
            return final_fill_price

        total_qty = 0
        total_cost = 0.0
        for f in partial_fills:
            q = int(f.get('qty') or 0)
            p = float(f.get('price') or 0.0)
            total_qty += q
            total_cost += q * p

        if final_fill_price is not None and final_qty is not None and final_qty > 0:
            total_qty += final_qty
            total_cost += final_qty * float(final_fill_price)

        if total_qty > 0:
            return round(total_cost / total_qty, 4)
        return final_fill_price

    def handle_partial_stop_fill(
        self,
        active: Dict[str, Any],
        *,
        broker_result: OrderResult,
    ) -> None:
        if self._broker_actions_blocked():
            return
        if self.state.get('status') in ('closing', 'closed'):
            return
        if self.state.get('short_closed_at'):
            return

        oid = str(active.get('order_id') or (self.state.get('active_stop') or {}).get('order_id') or '')
        if not oid:
            return

        partial_qty = int(broker_result.filled_quantity or 0)
        partial_price = float(broker_result.filled_price or broker_result.short_fill_price or active.get('stop_price') or 0.0)
        log.warning(
            'Partial stop fill detected for %s on order %s (%d/%s filled @ %s). Cancelling stop to exit remaining quantity at market mid.',
            self.json_path,
            oid,
            partial_qty,
            broker_result.order_quantity or self.state.get('stop_quantity'),
            partial_price,
        )

        outcome = self._cancel_stop_and_confirm(oid, timeout=3.0)

        latest = self.broker.get_order_status(
            oid,
            priority=PRIORITY_HIGH,
            op=OPERATION_STOP_CANCEL,
        )
        if latest.success:
            if latest.filled_quantity is not None and latest.filled_quantity > partial_qty:
                partial_qty = int(latest.filled_quantity)
            if latest.filled_price is not None:
                partial_price = float(latest.filled_price)

        short_sym = self.state['short_leg']['symbol']
        held_qty = None
        try:
            held_qty = self.broker.get_option_position_quantity(short_sym)
        except Exception as exc:
            log.warning('Could not get broker held position quantity for %s: %s', short_sym, exc)

        if held_qty is not None:
            remaining_qty = abs(held_qty) if held_qty < 0 else 0
        else:
            total_entry = int(self.state.get('filled_quantity') or self.state.get('quantity') or 0)
            already_closed = sum(int(p.get('qty') or 0) for p in self.state.get('short_partial_fills', []))
            remaining_qty = max(0, total_entry - already_closed - partial_qty)

        # Record this partial fill in state
        partial_fills = self.state.setdefault('short_partial_fills', [])
        already_recorded = any(str(p.get('order_id')) == str(oid) for p in partial_fills)
        if not already_recorded and partial_qty > 0:
            partial_fills.append({
                'order_id': str(oid),
                'qty': partial_qty,
                'price': partial_price,
                'timestamp': state_mod.now_iso(),
            })

        state_mod.append_stop_history(
            self.state,
            action='partial_fill_cancelled',
            order_id=oid,
            price=partial_price,
            phase=active.get('phase', 1),
            reason=f'partial_fill:{partial_qty}_filled_rem_{remaining_qty}',
            spx_price_at_event=self.prices.get_spx(),
        )

        if remaining_qty <= 0:
            fill_px = self._calculate_blended_short_close_price(partial_price, final_qty=partial_qty)
            self.state['short_close_price'] = fill_px
            self.state['short_closed_at'] = time.time()
            self.state['status'] = 'closing'
            self.state['short_remaining_qty'] = 0
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0
            if not self.state.get('close_mechanism'):
                self.state['close_mechanism'] = 'exchange_stop'
            state_mod.save_state(self.json_path, self.state)
            log.info(
                'All short contracts closed via stop (%s) — status=closing, long close in %ss',
                self.json_path,
                self.long_close_delay_sec,
            )
            return

        self.state['short_remaining_qty'] = remaining_qty
        self.state['stop_quantity'] = remaining_qty

        short_p = self.prices.get_market_mid(short_sym)
        if short_p is None:
            short_p = self.prices.get(short_sym)
        if short_p is None and active.get('stop_price'):
            short_p = float(active['stop_price'])
        if short_p is None:
            log.error('Partial stop fill: missing MQTT price for %s to place market mid limit close', short_sym)
            self.state['active_stop'] = None
            state_mod.save_state(self.json_path, self.state)
            return

        limit_price = round_spx_option_price(short_p)
        result = self.broker.place_limit_order(
            'BUY_TO_CLOSE',
            short_sym,
            remaining_qty,
            limit_price,
        )
        if not result.success:
            log.error('Partial stop fill: limit close failed: %s', result.message)
            self.state['active_stop'] = None
            state_mod.save_state(self.json_path, self.state)
            return

        new_oid = str(result.order_id)
        self.state['active_stop'] = {
            'order_id': new_oid,
            'type': 'LIMIT',
            'stop_price': None,
            'limit_price': limit_price,
            'phase': 1,
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': remaining_qty,
            'reason': 'partial_stop_fill_market_mid_limit',
        }
        self.state['close_mechanism'] = 'partial_stop_fill_exit'
        self._reregister_alert(oid, new_oid)
        state_mod.append_stop_history(
            self.state,
            action='replaced_limit',
            order_id=new_oid,
            price=limit_price,
            phase=1,
            reason=f'partial_stop_fill_mid_limit_qty_{remaining_qty}',
            spx_price_at_event=self.prices.get_spx(),
        )
        state_mod.save_state(self.json_path, self.state)
        log.info(
            'Partial stop fill: placed limit close %s @ %s for remaining %d contracts on %s',
            new_oid,
            limit_price,
            remaining_qty,
            short_sym,
        )
        if str(result.status).lower() == 'filled':
            self._sync_active_close_order()

    def handle_stop_order_update(
        self,
        stop: Dict[str, Any],
        *,
        broker_result: Optional[OrderResult] = None,
    ) -> None:
        if self.state.get('status') in ('closing', 'closed'):
            return
        if self.state.get('short_closed_at'):
            return

        oid = str(stop.get('order_id') or (self.state.get('active_stop') or {}).get('order_id') or '')
        if not oid:
            return

        result = broker_result
        if result is None:
            result = self.broker.get_order_status(
                oid,
                priority=PRIORITY_HIGH,
                op=OPERATION_SPREAD_CLOSE_STATUS,
            )
        if not stop_order_fully_filled(self.state, result):
            if result.filled_quantity and int(result.filled_quantity) > 0:
                active = self.state.get('active_stop') or stop
                self.handle_partial_stop_fill(active, broker_result=result)
                return
            active = self.state.get('active_stop') or {}
            if active.get('order_id') == oid and result.success:
                active['status'] = result.status
            log.info(
                'Stop %s not fully filled yet (%s/%s, status=%s) — long leg deferred',
                oid,
                result.filled_quantity if result.success else '?',
                result.order_quantity if result.success else stop_qty_for_state(self.state),
                result.status if result.success else 'unknown',
            )
            return

        active = self.state.get('active_stop') or {}
        if active.get('order_id') == stop.get('order_id') or active.get('order_id') == oid:
            active['status'] = 'filled'

        is_oco = active.get('type') == 'OCO'
        pt_price = float(active.get('profit_price') or 0.15) if is_oco else None

        fill_px = None
        if broker_result and broker_result.success and broker_result.filled_price is not None:
            fill_px = float(broker_result.filled_price)
        if fill_px is None:
            status_result = self.broker.get_order_status(
                oid,
                priority=PRIORITY_HIGH,
                op=OPERATION_SPREAD_CLOSE_STATUS,
            )
            if status_result.success and status_result.filled_price is not None:
                fill_px = float(status_result.filled_price)

        is_pt_fill = False
        if is_oco and pt_price is not None and fill_px is not None:
            if fill_px <= pt_price + 0.05:
                is_pt_fill = True

        is_pt_limit = (
            self.state.get('close_mechanism') == 'profit_target'
            or 'profit_target' in str(active.get('reason') or '')
        )
        if is_pt_fill or is_pt_limit:
            reason = 'profit_target_oco' if is_pt_fill else 'profit_target'
            close_mech = 'profit_target_oco' if is_pt_fill else 'profit_target'
        else:
            reason = 'stop_filled'
            close_mech = 'exchange_stop'

        spx = self.prices.get_spx()
        state_mod.append_stop_history(
            self.state,
            action='filled',
            order_id=stop.get('order_id'),
            price=fill_px if fill_px is not None else stop.get('stop_price'),
            phase=stop.get('phase', 1),
            reason=reason,
            spx_price_at_event=spx,
            status='filled',
        )
        if active.get('limit_price') is not None:
            self.state['short_close_limit_price'] = float(active['limit_price'])
        elif active.get('stop_price') is not None:
            self.state['short_close_limit_price'] = float(active['stop_price'])

        if fill_px is not None:
            fill_px = self._calculate_blended_short_close_price(
                fill_px, final_qty=int(active.get('quantity') or self.state.get('stop_quantity') or 1)
            )
        self.state['short_close_price'] = (
            fill_px
            if fill_px is not None
            else active.get('limit_price') or stop.get('stop_price') or stop.get('limit_price')
        )
        if not self.state.get('close_mechanism'):
            self.state['close_mechanism'] = close_mech
        self.state['status'] = 'closing'
        self.state['short_remaining_qty'] = 0
        fill_ts = time.time()
        if broker_result is not None and broker_result.filled_at is not None:
            fill_ts = float(broker_result.filled_at)
        elif broker_result is not None and broker_result.raw is not None:
            from blocks.stop.v3.recovery import fill_timestamp_from_broker_result
            parsed = fill_timestamp_from_broker_result(broker_result)
            if parsed is not None:
                fill_ts = parsed
        self.state['short_closed_at'] = fill_ts
        state_mod.save_state(self.json_path, self.state)
        log.info(
            'Short leg closed (%s) — status=closing, long close in %ss',
            close_mech,
            self.long_close_delay_sec,
        )

    def _resolve_long_close_quote(self) -> LegQuoteResult:
        long_sym = self.state['long_leg']['symbol']
        return resolve_leg_mid(long_sym, self.prices, self.broker)

    def _mark_long_close_blocked(self, source: str) -> None:
        self.state['long_close_source'] = source
        self.state['exit_last_step'] = 'long_close_blocked_no_quote'
        log.critical(
            'Long close blocked — no quote (source=%s) for %s',
            source,
            self.json_path,
        )

    def _compute_long_close_limit(self, existing_limit: Optional[float] = None) -> Optional[float]:
        """
        SELL_TO_CLOSE limit from MQTT/REST mid.

        When the rounded mid matches (or is above) the working limit, step down one
        SPX tick instead of re-sending the same price.
        """
        quote = self._resolve_long_close_quote()
        if quote.mid is None:
            return None
        if quote.mid < 0.10:
            mid_limit = LEGACY_LONG_CLOSE_FLOOR
        else:
            mid_limit = round_spx_option_price(quote.mid)
        if existing_limit is None:
            return mid_limit
        existing = round_spx_option_price(float(existing_limit))
        if existing <= LEGACY_LONG_CLOSE_FLOOR and mid_limit > existing:
            return mid_limit
        if mid_limit >= existing:
            return step_down_spx_option_price(existing)
        return mid_limit

    def _place_long_close_limit(self, limit_price: float, *, source: Optional[str] = None) -> bool:
        """Place SELL_TO_CLOSE at limit_price; return True if broker accepted."""
        quote = self._resolve_long_close_quote()
        if quote.mid is None:
            self._mark_long_close_blocked(quote.source)
            return False
        if limit_price <= LEGACY_LONG_CLOSE_FLOOR and quote.mid >= 0.15:
            self._mark_long_close_blocked('blocked_no_quote')
            log.critical(
                'Refusing long close at floor %.2f — live quote %.2f (source=%s)',
                limit_price,
                quote.mid,
                quote.source,
            )
            return False
        long_sym = self.state['long_leg']['symbol']
        try:
            held_qty = self.broker.get_single_leg_position_qty(long_sym)
            if held_qty <= 0:
                log.warning(
                    'Cannot place long close: position for %s is already flat/short (qty=%d) — auto-finalizing close',
                    long_sym,
                    held_qty,
                )
                self._finalize_close(reason=self.state.get('close_mechanism') or 'position_flat')
                return False
        except Exception as exc:
            log.warning('Broker position verification check failed in _place_long_close_limit: %s', exc)
            held_qty = None

        desired_qty = stop_qty_for_state(self.state)
        qty = min(desired_qty, held_qty) if held_qty is not None and held_qty > 0 else desired_qty
        limit_price = round_spx_option_price(limit_price)
        result = self.broker.place_limit_order('SELL_TO_CLOSE', long_sym, qty, limit_price)
        if result.success:
            self.state['long_close_order_id'] = result.order_id
            self.state['long_close_limit_price'] = limit_price
            self.state['long_close_attempts'] = self.state.get('long_close_attempts', 0) + 1
            self.state['long_close_source'] = source or quote.source
            self.state.pop('long_close_fail_count', None)
            self.state.pop('long_close_last_error', None)
            self.state.pop('long_close_next_retry_at', None)
            log.info(
                'Long close placed: %s qty=%s limit=%s (order %s, attempt %d, source=%s)',
                long_sym,
                qty,
                limit_price,
                result.order_id,
                self.state['long_close_attempts'],
                self.state.get('long_close_source', source or '?'),
            )
            return True
        fail_count = int(self.state.get('long_close_fail_count') or 0) + 1
        self.state['long_close_fail_count'] = fail_count
        self.state['long_close_last_error'] = str(result.message)
        self.state['long_close_failed_at'] = state_mod.now_iso()
        backoff_sec = min(60.0, 5.0 * (2 ** min(fail_count - 1, 4)))
        self.state['long_close_next_retry_at'] = time.time() + backoff_sec
        log.error(
            'Long close placement failed (fail_count=%d, backoff=%.1fs): %s',
            fail_count,
            backoff_sec,
            result.message,
        )
        return False

    def _close_long_leg(self, quantity: Optional[int] = None) -> None:
        if quantity is not None:
            log.warning('_close_long_leg quantity override ignored; using stop_qty_for_state')
        self._place_long_close_at_mid()

    def _finalize_close(self, reason: str, spx_price: Optional[float] = None) -> None:
        from blocks.stop.close_fills import apply_close_slippage_fields
        from blocks.stop.v3.recovery import finalize_v3_exit_state

        short_fill = self.state['short_leg']['fill_price']
        long_fill = self.state['long_leg']['fill_price']
        entry_credit = self.state['entry']['net_credit']
        self.state['status'] = 'closed'
        self.state['close'] = {
            'reason': reason,
            'timestamp': state_mod.now_iso(),
            'entry_credit': entry_credit,
            'short_fill': short_fill,
            'long_fill': long_fill,
            'short_close_price': self.state.get('short_close_price'),
            'long_close_price': self.state.get('long_close_price'),
            'short_close_limit_price': self.state.get('short_close_limit_price'),
            'long_close_limit_price': self.state.get('long_close_limit_price'),
            'close_mechanism': self.state.get('close_mechanism'),
            'spx_at_close': spx_price,
        }
        apply_close_slippage_fields(self.state)
        finalize_v3_exit_state(self.state)
        state_mod.move_to_closed(self.json_path, self.state)
        log.info('Spread closed: %s (%s)', self.json_path, reason)

    # --- Long close chase/replace loop (GAP-03) ---

    def _chase_long_close(self) -> None:
        """Check long close order and reprice if needed."""
        retry_at = self.state.get('long_close_next_retry_at')
        if retry_at and time.time() < float(retry_at):
            return

        oid = self.state.get('long_close_order_id')
        fail_count = int(self.state.get('long_close_fail_count') or 0)

        # If repeated order placement failures occur with no working order, check broker position
        if not oid and fail_count >= 3:
            long_sym = self.state['long_leg']['symbol']
            try:
                held_qty = self.broker.get_single_leg_position_qty(long_sym)
                if held_qty <= 0:
                    log.warning(
                        'Long leg %s is flat (qty=%d) at broker after %d placement failures — auto-finalizing close',
                        long_sym,
                        held_qty,
                        fail_count,
                    )
                    self._finalize_close(reason=self.state.get('close_mechanism') or 'position_flat')
                    return
            except Exception as exc:
                log.warning('Broker position verification check failed: %s', exc)

        quote = self._resolve_long_close_quote()
        existing_limit = self.state.get('long_close_limit_price')

        if not oid:
            if quote.mid is None:
                self._mark_long_close_blocked(quote.source)
                return
            attempts = self.state.get('long_close_attempts', 0)
            if attempts >= 10 and self.state.get('long_close_source') in ('mqtt', 'broker_rest'):
                self._place_long_close_market()
            else:
                self._place_long_close_at_mid()
            return

        result = self.broker.get_order_status(
            oid,
            priority=PRIORITY_HIGH,
            op=OPERATION_LONG_CLOSE_STATUS,
        )
        if not result.success:
            return

        status = str(result.status).lower()
        if status == 'filled':
            if result.filled_price is not None:
                self.state['long_close_price'] = float(result.filled_price)
            self._finalize_close(reason=self.state.get('close_mechanism') or 'stop_filled')
            return

        if status in ('cancelled', 'canceled', 'rejected', 'expired'):
            self.state.pop('long_close_order_id', None)
            if quote.mid is None:
                self._mark_long_close_blocked(quote.source)
                return
            attempts = self.state.get('long_close_attempts', 0)
            if attempts >= 10 and self.state.get('long_close_source') in ('mqtt', 'broker_rest'):
                self._place_long_close_market()
            else:
                self._place_long_close_at_mid(existing_limit=existing_limit)
            return

        if quote.mid is None:
            self._mark_long_close_blocked(quote.source)
            return

        attempts = self.state.get('long_close_attempts', 0)
        if attempts >= 10 and self.state.get('long_close_source') in ('mqtt', 'broker_rest'):
            log.info('Long close escalating to MARKET — cancelling working order %s first', oid)
            self.broker.cancel_order(oid)
            return

        if existing_limit is not None:
            existing_rounded = round_spx_option_price(float(existing_limit))
            if existing_rounded <= LEGACY_LONG_CLOSE_FLOOR and quote.mid >= 0.15:
                new_limit = round_spx_option_price(quote.mid)
                log.info('Long close floor exit — cancelling order %s to re-place at %.2f', oid, new_limit)
                self.broker.cancel_order(oid)
                return

        if quote.mid <= 0.01:
            return

        new_limit = self._compute_long_close_limit(existing_limit)
        if new_limit is None:
            self._mark_long_close_blocked('blocked_no_quote')
            return
        if existing_limit is not None and new_limit >= round_spx_option_price(float(existing_limit)):
            return

        log.info('Long close repricing step — cancelling order %s to re-place at %.2f', oid, new_limit)
        self.broker.cancel_order(oid)

    def _place_long_close_at_mid(self, existing_limit: Optional[float] = None) -> None:
        """Place SELL_TO_CLOSE; step down if mid matches the last working limit."""
        quote = self._resolve_long_close_quote()
        if quote.mid is None:
            self._mark_long_close_blocked(quote.source)
            return
        limit = existing_limit if existing_limit is not None else self.state.get('long_close_limit_price')
        limit_price = self._compute_long_close_limit(limit)
        if limit_price is None:
            self._mark_long_close_blocked('blocked_no_quote')
            return
        if limit is not None:
            limit_rounded = round_spx_option_price(float(limit))
            if (
                limit_price >= limit_rounded
                and not (limit_rounded <= LEGACY_LONG_CLOSE_FLOOR and limit_price > limit_rounded)
            ):
                log.info('Long close skip re-place at same limit %.2f', limit_price)
                return
        self._place_long_close_limit(limit_price, source=quote.source)

    def _place_long_close_market(self) -> None:
        """Escalate to market order after too many chase attempts."""
        long_sym = self.state['long_leg']['symbol']
        try:
            held_qty = self.broker.get_single_leg_position_qty(long_sym)
            if held_qty <= 0:
                log.warning(
                    'Cannot place long close market: position for %s is already flat/short (qty=%d) — auto-finalizing close',
                    long_sym,
                    held_qty,
                )
                self._finalize_close(reason=self.state.get('close_mechanism') or 'position_flat')
                return
        except Exception as exc:
            log.warning('Broker position verification check failed in _place_long_close_market: %s', exc)
            held_qty = None

        desired_qty = stop_qty_for_state(self.state)
        qty = min(desired_qty, held_qty) if held_qty is not None and held_qty > 0 else desired_qty
        result = self.broker.place_market_order('SELL_TO_CLOSE', long_sym, qty)
        if result.success:
            self.state['long_close_order_id'] = result.order_id
            self.state.pop('long_close_fail_count', None)
            self.state.pop('long_close_last_error', None)
            self.state.pop('long_close_next_retry_at', None)
            log.info('Long close escalated to MARKET: %s qty=%s (order %s)',
                     long_sym, qty, result.order_id)
        else:
            fail_count = int(self.state.get('long_close_fail_count') or 0) + 1
            self.state['long_close_fail_count'] = fail_count
            self.state['long_close_last_error'] = str(result.message)
            self.state['long_close_failed_at'] = state_mod.now_iso()
            backoff_sec = min(60.0, 5.0 * (2 ** min(fail_count - 1, 4)))
            self.state['long_close_next_retry_at'] = time.time() + backoff_sec
            log.error('Long close market order failed: %s', result.message)

    # --- AlertListener re-registration (GAP-04) ---

    def _reregister_alert(self, old_oid: Optional[str], new_oid: Optional[str]) -> None:
        """Re-register AlertListener when stop order ID changes."""
        if not self.alert_listener:
            return
        try:
            if old_oid:
                self.alert_listener.unregister(old_oid)
            if new_oid:
                new_q = self.alert_listener.register(new_oid)
                self.fill_queue = new_q
        except Exception as e:
            log.warning('AlertListener re-register failed: %s', e)
