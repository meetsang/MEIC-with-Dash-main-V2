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
from blocks.stop.broker_sync import cancel_all_close_orders_on_short
from blocks.stop.fill_sync import (
    stop_is_current,
    stop_order_fully_filled,
    stop_qty_for_state,
    sync_open_order,
)
from blocks.stop.mqtt_prices import MqttPriceCache
from common.market_hours import (
    MARKET_CLOSE_HOUR_CT,
    MARKET_CLOSE_MINUTE_CT,
    trade_past_0dte_close,
)
from blocks.stop.breach_watch import build_breach_watch_snapshot, log_breach_watch
from blocks.stop.stop_math import apply_two_x_thresholds, exchange_stop_limit_prices, exchange_stop_price, spread_breach_threshold, stop_multiplier_for_state
from common.streamer_health import is_stale as streamer_prices_stale
from blocks.stop.stop_profile import StopProfile, resolve_stop_profile

if TYPE_CHECKING:
    from blocks.stop.alerts import AlertListener

log = logging.getLogger(__name__)

FAST_INTERVAL = 3       # seconds — breach check + long chase

def _trades_dir() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    return os.path.join(root, 'trades')


def _trades_root_for_path(json_path: str) -> str:
    """Parent of active/ for the trade file (trades/ in V2 layout)."""
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
        self._breach_missing_prices_logged = False
        self._breach_stale_logged = False
        self._breach_last_near_log = 0.0
        self._breach_last_near_spread: Optional[float] = None

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

    def _broker_actions_frozen(self) -> bool:
        if not self._0dte_past_market_close():
            return False
        self._log_0dte_freeze_once()
        return True

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
        while not self.fill_queue.empty():
            try:
                event = self.fill_queue.get_nowait()
                order_id = str(event.get('order_id', ''))
                active = self.state.get('active_stop') or {}
                if active.get('order_id') == order_id:
                    status = str(event.get('status', 'filled')).lower()
                    active['status'] = status
                    if status == 'filled':
                        result = self.broker.get_order_status(order_id)
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

        active = self.state.get('active_stop')
        if active and active.get('order_id'):
            result = self.broker.get_order_status(active['order_id'])
            if result.success:
                st = str(result.status).lower()
                if st in ('cancelled', 'canceled'):
                    self._reconcile_active_stop_with_broker()
                else:
                    active['status'] = result.status
                    if stop_order_fully_filled(self.state, result):
                        self.handle_stop_order_update(active, broker_result=result)
                self._maybe_merge_disk_stop_state()
                state_mod.save_state(self.json_path, self.state)
        self._ensure_stop_for_filled_qty()
        if self.state.get('status') == 'open' and not stop_is_current(self.state):
            state_mod.save_state(self.json_path, self.state)

        self._recover_closing_on_load()

    def _recover_closing_on_load(self) -> None:
        """G8: resume long close after stop_monitor restart."""
        if self.state.get('status') != 'closing':
            return

        if self.state.get('spread_close_order_id'):
            self._poll_spread_close()
            return

        oid = self.state.get('long_close_order_id')
        if oid:
            result = self.broker.get_order_status(str(oid))
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

    def _cancel_all_close_orders_on_short(self) -> None:
        cancel_all_close_orders_on_short(self.state, self.broker)

    def _ensure_stop_for_filled_qty(self) -> None:
        """Place or resize exchange stop only for filled spread units (both legs)."""
        if state_mod.section(self.state, 'active_stop').get('skip_initial'):
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

        if active.get('order_id') and int(self.state.get('stop_quantity') or 0) < filled:
            self._resize_stop(filled)
            return

        if not active.get('order_id'):
            self.setup_initial_stop()

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
        result = self.broker.get_order_status(str(oid))
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

    def replace_with_spread_close(self, reason: str = 'manual_close') -> None:
        """Kill path — close both legs in one spread order (not leg-by-leg)."""
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if oid:
            outcome = self._cancel_stop_and_confirm(oid)
            if outcome == 'filled':
                result = self.broker.get_order_status(str(oid))
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
        short_p = self.prices.get_market_mid(short_sym) or self.prices.get(short_sym)
        long_p = self.prices.get_market_mid(long_sym) or self.prices.get(long_sym)
        if short_p is None or long_p is None:
            log.error('Spread close: missing MQTT quotes for %s / %s', short_sym, long_sym)
            return

        raw_debit = max(float(short_p) - float(long_p), 0.05)
        debit = round_spx_option_price(raw_debit + app_config.OPEN_PRICE_ADJ)
        result = self.broker.place_spread_close_order(short_sym, long_sym, qty, debit)
        if not result.success:
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

        mult = float(cmd.get('stop_multiplier', 2))
        self.state['stop_multiplier'] = mult
        apply_two_x_thresholds(self.state, mult)

        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if oid:
            outcome = self._cancel_stop_and_confirm(oid)
            if outcome == 'filled':
                result = self.broker.get_order_status(str(oid))
                self.handle_stop_order_update(active, broker_result=result)
                try:
                    os.unlink(cmd_path)
                except Exception:
                    pass
                return True
            self.state['active_stop'] = None
            self.state['stop_quantity'] = 0

        stop_price = round_spx_option_price(float(self.state['short_leg']['two_x_short']))
        limit_price = round_spx_option_price(stop_price + app_config.LIMIT_OFFSET)
        self._place_short_stop(stop_price, limit_price, phase=1, reason='stop_update')
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

    def _poll_once(self) -> None:
        """Fast path: breach detection + long close chase. Runs every ~3s."""
        if self._broker_actions_frozen():
            self._maybe_merge_disk_stop_state()
            state_mod.save_state(self.json_path, self.state)
            return

        now = time.time()

        # Check dashboard kill switch or per-trade close commands first
        if self._check_dashboard_commands():
            return

        if self._check_stop_update_command():
            return

        if self._poll_spread_close():
            return

        if self.state.get('status') == 'open':
            if not stop_is_current(self.state):
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
        self._refresh_breach_watch(streamer_stale)

        if streamer_stale:
            self._maybe_merge_disk_stop_state()
            state_mod.save_state(self.json_path, self.state)
            return

        # Slow path: broker sync (every SLOW_INTERVAL)
        if now - self._last_broker_sync >= SLOW_INTERVAL:
            self._last_broker_sync = now
            self._reconcile_active_stop_with_broker()
            self._sync_working_stop_order()

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

    def _refresh_breach_watch(self, streamer_stale: bool) -> None:
        """Persist spread vs software breach threshold; rate-limited diagnostics."""
        short_sym = self.state['short_leg']['symbol']
        long_sym = self.state['long_leg']['symbol']
        short_p = self.prices.get(short_sym)
        long_p = self.prices.get(long_sym)
        watch = build_breach_watch_snapshot(
            self.state,
            short_p=short_p,
            long_p=long_p,
            streamer_stale=streamer_stale,
            now_iso=state_mod.now_iso(),
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
        qty = qty if qty is not None else stop_qty_for_state(self.state)
        if qty <= 0:
            return False
        stop_price = round_spx_option_price(stop_price)
        limit_price = round_spx_option_price(limit_price)

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
            self._stop_place_backoff_until = time.time() + 60
            return False

        spx = self.prices.get_spx()
        self.state['active_stop'] = {
            'order_id': result.order_id,
            'type': 'STOP_LIMIT',
            'stop_price': stop_price,
            'limit_price': limit_price,
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
            order_id=result.order_id,
            price=stop_price,
            phase=phase,
            reason=reason,
            spx_price_at_event=spx,
        )
        state_mod.save_state(self.json_path, self.state)
        log.info('Placed stop %s @ %s (%s)', result.order_id, stop_price, reason)
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
        """Poll broker for STOP_LIMIT / LIMIT fill (exchange stop or breach close)."""
        active = self.state.get('active_stop') or {}
        if not active.get('order_id'):
            return
        if active.get('type') not in ('STOP_LIMIT', 'LIMIT'):
            return
        if str(active.get('status', '')).lower() in ('filled', 'cancelled', 'rejected'):
            return
        self._sync_active_close_order()

    def _reconcile_active_stop_with_broker(self) -> None:
        """Adopt a manually replaced stop when JSON still points at a cancelled order."""
        if self.state.get('status') != 'open':
            return

        short_sym = self.state['short_leg']['symbol']
        active = self.state.get('active_stop') or {}
        oid = str(active.get('order_id') or '')

        if oid:
            result = self.broker.get_order_status(oid)
            if result.success:
                st = str(result.status).lower()
                if st in ('working', 'live', 'contingent', 'received', 'open', 'partially filled'):
                    active['status'] = 'working'
                    return
                if stop_order_fully_filled(self.state, result):
                    self.handle_stop_order_update(active, broker_result=result)
                    return
                if st not in ('cancelled', 'canceled', 'rejected', 'expired', 'unknown'):
                    return

        working = self.broker.find_working_close_order(short_sym)
        if not working or not working.order_id:
            return

        new_oid = str(working.order_id)
        if oid == new_oid and str(active.get('status', '')).lower() in ('working', 'live'):
            return

        raw = working.raw
        stop_price = active.get('stop_price')
        limit_price = active.get('limit_price')
        if raw is not None:
            trig = getattr(raw, 'stop_trigger', None)
            if trig is not None:
                stop_price = round_spx_option_price(float(trig))
            if getattr(raw, 'price', None) is not None:
                limit_price = round_spx_option_price(abs(float(raw.price)))

        old_oid = oid or None
        qty = int(
            working.order_quantity
            or working.filled_quantity
            or stop_qty_for_state(self.state)
        )
        self.state['active_stop'] = {
            'order_id': new_oid,
            'type': 'STOP_LIMIT',
            'stop_price': stop_price,
            'limit_price': limit_price,
            'phase': active.get('phase', 1),
            'status': 'working',
            'placed_at': state_mod.now_iso(),
            'quantity': qty,
        }
        self.state['stop_quantity'] = qty
        if stop_price is not None:
            self.state['designated_stop_price'] = float(stop_price)
        if old_oid and old_oid != new_oid:
            state_mod.append_stop_history(
                self.state,
                action='cancelled',
                order_id=old_oid,
                price=active.get('stop_price'),
                phase=active.get('phase', 1),
                reason='replaced_at_broker',
                spx_price_at_event=self.prices.get_spx(),
            )
        state_mod.append_stop_history(
            self.state,
            action='placed',
            order_id=new_oid,
            price=stop_price,
            phase=active.get('phase', 1),
            reason='broker_reconcile',
            spx_price_at_event=self.prices.get_spx(),
        )
        self._reregister_alert(old_oid, new_oid)
        log.info('Reconciled active stop for %s → order %s', short_sym, new_oid)
        state_mod.save_state(self.json_path, self.state)

    def _maybe_merge_disk_stop_state(self) -> None:
        """Adopt manual stop JSON edits when in-memory state is stale."""
        try:
            disk = state_mod.load_state(self.json_path)
        except Exception:
            return

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

    def _sync_active_close_order(self) -> None:
        """Poll broker for fill/cancel on the working short-leg close order."""
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        if not oid:
            return
        result = self.broker.get_order_status(str(oid))
        if not result.success:
            return
        active['status'] = result.status
        if stop_order_fully_filled(self.state, result):
            self.handle_stop_order_update(active, broker_result=result)
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
        status = self.broker.get_order_status(oid)
        if status.success and str(status.status).lower() == 'filled':
            return 'filled'

        cancel = self.broker.cancel_order(oid)
        if not cancel.success and str(cancel.status).lower() == 'filled':
            return 'filled'

        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.broker.get_order_status(oid)
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
        active = self.state.get('active_stop') or {}
        oid = active.get('order_id')
        phase = active.get('phase', 1)

        if not oid:
            log.error('Breach limit close: no active stop in JSON or broker — JSON unchanged')
            return

        outcome = self._cancel_stop_and_confirm(oid)
        if outcome == 'filled':
            result = self.broker.get_order_status(str(oid))
            self.handle_stop_order_update(active, broker_result=result)
            return
        if outcome != 'cancelled':
            log.error(
                'Breach limit close: stop %s not cancelled at broker — JSON unchanged',
                oid,
            )
            return

        state_mod.append_stop_history(
            self.state,
            action='cancelled',
            order_id=oid,
            price=active.get('stop_price') or active.get('limit_price'),
            phase=phase,
            reason=f'breach_cancel:{reason}',
            spx_price_at_event=self.prices.get_spx(),
        )

        short_sym = self.state['short_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        short_p = self.prices.get_market_mid(short_sym)
        if short_p is None:
            short_p = self.prices.get(short_sym)
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
        self.state['stop_quantity'] = qty
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
            result = self.broker.get_order_status(oid)
        if not stop_order_fully_filled(self.state, result):
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

        spx = self.prices.get_spx()
        state_mod.append_stop_history(
            self.state,
            action='filled',
            order_id=stop.get('order_id'),
            price=stop.get('stop_price'),
            phase=stop.get('phase', 1),
            reason='stop_filled',
            spx_price_at_event=spx,
            status='filled',
        )
        active = self.state.get('active_stop') or {}
        if active.get('limit_price') is not None:
            self.state['short_close_limit_price'] = float(active['limit_price'])
        elif active.get('stop_price') is not None:
            self.state['short_close_limit_price'] = float(active['stop_price'])

        fill_px = None
        if broker_result and broker_result.success and broker_result.filled_price is not None:
            fill_px = float(broker_result.filled_price)
        if fill_px is None:
            status_result = self.broker.get_order_status(oid)
            if status_result.success and status_result.filled_price is not None:
                fill_px = float(status_result.filled_price)
        self.state['short_close_price'] = (
            fill_px
            if fill_px is not None
            else active.get('limit_price') or stop.get('stop_price') or stop.get('limit_price')
        )
        if not self.state.get('close_mechanism'):
            self.state['close_mechanism'] = 'exchange_stop'
        self.state['status'] = 'closing'
        self.state['short_closed_at'] = time.time()
        state_mod.save_state(self.json_path, self.state)
        log.info(
            'Short leg closed — status=closing, long close in %ss',
            self.long_close_delay_sec,
        )

    def _long_leg_mid(self) -> float:
        long_sym = self.state['long_leg']['symbol']
        long_p = self.prices.get_market_mid(long_sym) or self.prices.get(long_sym)
        if long_p is not None and float(long_p) > 20.0:
            log.error(
                'Long leg mid %.2f for %s looks like index noise — using $0.05 floor',
                long_p,
                long_sym,
            )
            return 0.05
        return float(long_p) if long_p is not None else 0.05

    def _compute_long_close_limit(self, existing_limit: Optional[float] = None) -> float:
        """
        SELL_TO_CLOSE limit from MQTT mid.

        When the rounded mid matches (or is above) the working limit, step down one
        SPX tick instead of re-sending the same price ($0.05 below $3, $0.10 at/above).
        """
        mid_limit = round_spx_option_price(self._long_leg_mid())
        if existing_limit is None:
            return mid_limit
        existing = round_spx_option_price(float(existing_limit))
        if mid_limit >= existing:
            return step_down_spx_option_price(existing)
        return mid_limit

    def _place_long_close_limit(self, limit_price: float) -> bool:
        """Place SELL_TO_CLOSE at limit_price; return True if broker accepted."""
        long_sym = self.state['long_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        limit_price = round_spx_option_price(limit_price)
        result = self.broker.place_limit_order('SELL_TO_CLOSE', long_sym, qty, limit_price)
        if result.success:
            self.state['long_close_order_id'] = result.order_id
            self.state['long_close_limit_price'] = limit_price
            self.state['long_close_attempts'] = self.state.get('long_close_attempts', 0) + 1
            log.info(
                'Long close placed: %s qty=%s limit=%s (order %s, attempt %d)',
                long_sym,
                qty,
                limit_price,
                result.order_id,
                self.state['long_close_attempts'],
            )
            return True
        log.error('Long close re-place failed: %s', result.message)
        return False

    def _close_long_leg(self, quantity: Optional[int] = None) -> None:
        if quantity is not None:
            log.warning('_close_long_leg quantity override ignored; using stop_qty_for_state')
        self._place_long_close_at_mid()

    def _finalize_close(self, reason: str, spx_price: Optional[float] = None) -> None:
        from blocks.stop.close_fills import apply_close_slippage_fields

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
        state_mod.move_to_closed(self.json_path, self.state)
        log.info('Spread closed: %s (%s)', self.json_path, reason)

    # --- Long close chase/replace loop (GAP-03) ---

    def _chase_long_close(self) -> None:
        """Check long close order and reprice if needed."""
        oid = self.state.get('long_close_order_id')
        existing_limit = self.state.get('long_close_limit_price')

        if not oid:
            self._place_long_close_at_mid()
            return

        result = self.broker.get_order_status(oid)
        if not result.success:
            return

        status = str(result.status).lower()
        if status == 'filled':
            if result.filled_price is not None:
                self.state['long_close_price'] = float(result.filled_price)
            self._finalize_close(reason=self.state.get('close_mechanism') or 'stop_filled')
            return

        if status in ('cancelled', 'canceled', 'rejected', 'expired'):
            self._place_long_close_at_mid(existing_limit=existing_limit)
            return

        # Still working — reprice only when we can go one tick lower (more aggressive).
        if self._long_leg_mid() <= 0.01:
            return

        new_limit = self._compute_long_close_limit(existing_limit)
        if existing_limit is not None and new_limit >= round_spx_option_price(float(existing_limit)):
            return

        self.broker.cancel_order(oid)
        self._place_long_close_limit(new_limit)

        # Check attempt count for market order escalation
        attempts = self.state.get('long_close_attempts', 0)
        if attempts >= 10:
            self._place_long_close_market()

    def _place_long_close_at_mid(self, existing_limit: Optional[float] = None) -> None:
        """Place SELL_TO_CLOSE; step down if mid matches the last working limit."""
        limit = existing_limit if existing_limit is not None else self.state.get('long_close_limit_price')
        limit_price = self._compute_long_close_limit(limit)
        if limit is not None and limit_price >= round_spx_option_price(float(limit)):
            log.info('Long close skip re-place at same limit %.2f', limit_price)
            return
        self._place_long_close_limit(limit_price)

    def _place_long_close_market(self) -> None:
        """Escalate to market order after too many chase attempts."""
        long_sym = self.state['long_leg']['symbol']
        qty = stop_qty_for_state(self.state)
        result = self.broker.place_market_order('SELL_TO_CLOSE', long_sym, qty)
        if result.success:
            self.state['long_close_order_id'] = result.order_id
            log.info('Long close escalated to MARKET: %s qty=%s (order %s)',
                     long_sym, qty, result.order_id)
        else:
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
