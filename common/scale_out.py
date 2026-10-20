"""Scale-out / partial position close for vertical spreads (MEIC / MANUAL_SPREAD)."""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

from common.option_ticks import round_spx_option_price
from common import trades_layout
from blocks.stop import state as state_mod
from blocks.stop.v3.quotes import resolve_spread_close_debit

log = logging.getLogger(__name__)


def find_active_trade_file(filename: str, root: Optional[str] = None) -> Optional[str]:
    """Find active trade JSON file across strategy active subdirectories."""
    base_root = root or state_mod._project_root()
    active_base = os.path.join(base_root, 'trades', 'active')
    if not os.path.isdir(active_base):
        return None
    for dirpath, _, files in os.walk(active_base):
        if filename in files:
            return os.path.join(dirpath, filename)
    return None


class _PricesFeedWrapper:
    """Wraps dictionary or partial price sources so quote resolution can query get_market_mid."""

    def __init__(self, raw: Any):
        self._raw = raw or {}

    def get_market_mid(self, sym: str) -> Optional[float]:
        if hasattr(self._raw, 'get_market_mid'):
            return self._raw.get_market_mid(sym)
        if isinstance(self._raw, dict):
            return self._raw.get(sym)
        return None

    def get(self, sym: str, default=None):
        if hasattr(self._raw, 'get'):
            return self._raw.get(sym, default)
        return default

    def get_spx(self) -> Optional[float]:
        if hasattr(self._raw, 'get_spx'):
            return self._raw.get_spx()
        if isinstance(self._raw, dict):
            return self._raw.get('spx') or self._raw.get('SPX')
        return None


def _generate_child_lot(parent_lot: str, target_dir: str) -> str:
    """Generate next sub-lot suffix for split scale-out trades (e.g. ms-443-s1, ms-443-s2)."""
    prefix = f'{parent_lot}-s'
    existing_suffixes: list[int] = []
    if os.path.isdir(target_dir):
        for fname in os.listdir(target_dir):
            if fname.startswith(prefix):
                remainder = fname[len(prefix):]
                part = remainder.split('_')[0].split('.')[0]
                try:
                    existing_suffixes.append(int(part))
                except ValueError:
                    pass
    next_idx = max(existing_suffixes, default=0) + 1
    return f'{prefix}{next_idx}'


def create_scale_out_child_trade(
    parent_state: Dict[str, Any],
    parent_path: str,
    close_qty: int,
    close_res: Any,
    is_oco: bool,
    profit_debit: float,
    stop_trigger_debit: Optional[float],
    stop_limit_debit: Optional[float],
    actual_debit: float,
) -> tuple[str, str, Dict[str, Any]]:
    """
    Creates and saves a new child trade JSON representing the scaled-out portion.
    Returns (child_lot, child_filename, child_state).
    """
    import copy
    target_dir = os.path.dirname(os.path.abspath(parent_path))
    os.makedirs(target_dir, exist_ok=True)
    parent_lot = str(parent_state.get('lot') or 'ms-0')
    child_lot = _generate_child_lot(parent_lot, target_dir)

    parent_filename = os.path.basename(parent_path)
    name_part, ext = os.path.splitext(parent_filename)
    side = str(parent_state.get('entry', {}).get('side') or (parent_state.get('side') or 'P')).upper()
    if name_part.startswith(parent_lot):
        child_filename = f'{child_lot}{name_part[len(parent_lot):]}{ext}'
    else:
        child_filename = f'{child_lot}_{side}_{int(time.time())}{ext}'

    child_path = os.path.join(target_dir, child_filename)

    child_state = {
        'lot': child_lot,
        'parent_lot': parent_lot,
        'is_scale_out_split': True,
        'status': 'open',
        'strategy': parent_state.get('strategy') or 'MANUAL_SPREAD',
        'structure_type': parent_state.get('structure_type') or 'credit_spread',
        'quantity': close_qty,
        'filled_quantity': close_qty,
        'holding_quantity': close_qty,
        'initial_quantity': close_qty,
        'closed_quantity': 0,
        'designated_stop_price': stop_trigger_debit if is_oco else None,
        'stop_multiplier': parent_state.get('stop_multiplier', 4.0),
        'profit_target_pct': parent_state.get('profit_target_pct', 0.50),
        'entry': copy.deepcopy(parent_state.get('entry') or {}),
        'short_leg': copy.deepcopy(parent_state.get('short_leg') or {}),
        'long_leg': copy.deepcopy(parent_state.get('long_leg') or {}),
        'phases': copy.deepcopy(parent_state.get('phases') or {}),
        'active_stop': {
            'order_id': str(close_res.order_id) if getattr(close_res, 'order_id', None) else None,
            'type': 'SPREAD_OCO' if is_oco else 'SPREAD_LIMIT',
            'profit_debit': profit_debit,
            'profit_price': profit_debit,
            'stop_trigger_debit': stop_trigger_debit if is_oco else None,
            'stop_limit_debit': stop_limit_debit if is_oco else None,
            'stop_price': stop_trigger_debit if is_oco else None,
            'limit_price': stop_limit_debit if is_oco else None,
            'phase': 'OCO',
            'status': str(getattr(close_res, 'status', None) or ('working' if is_oco else 'filled')),
            'placed_at': state_mod.now_iso(),
            'quantity': close_qty,
            'symbol': parent_state.get('short_leg', {}).get('symbol', ''),
        },
        'stop_quantity': close_qty,
        'stop_history': [{
            'timestamp': state_mod.now_iso(),
            'action': 'placed_scale_out_oco' if is_oco else 'placed_scale_out_limit',
            'order_id': str(close_res.order_id) if getattr(close_res, 'order_id', None) else None,
            'price': stop_trigger_debit if is_oco else profit_debit,
            'phase': 'OCO',
            'reason': f'scale_out_split_from_{parent_lot}_qty_{close_qty}_pt_{profit_debit}_sl_{stop_trigger_debit}',
        }],
        'created_at': state_mod.now_iso(),
    }
    child_state['entry']['lot'] = child_lot
    child_state['entry']['quantity'] = close_qty
    child_state['entry']['filled_quantity'] = close_qty
    child_state['entry']['is_scale_out_split'] = True
    child_state['entry']['parent_lot'] = parent_lot

    is_filled = str(getattr(close_res, 'status', '') or '').lower() == 'filled'
    if is_filled:
        child_state['status'] = 'closed'
        child_state['closed_quantity'] = close_qty
        child_state['holding_quantity'] = 0
        child_state['short_close_price'] = getattr(close_res, 'short_fill_price', None)
        child_state['long_close_price'] = getattr(close_res, 'long_fill_price', None)
        child_state['close'] = {
            'reason': 'profit_target' if actual_debit <= profit_debit else 'scale_out_filled',
            'timestamp': state_mod.now_iso(),
            'entry_credit': child_state.get('entry', {}).get('net_credit'),
            'close_price': actual_debit,
            'close_mechanism': 'scale_out_oco',
            'quantity': close_qty,
        }
        state_mod.move_to_closed(child_path, child_state)
    else:
        state_mod.save_state(child_path, child_state)

    return child_lot, child_filename, child_state


def calculate_spread_stop_debit(
    state: Dict[str, Any],
    debit_target: float,
    stop_loss: Optional[float] = None,
) -> tuple[float, float]:
    """
    Calculate spread stop trigger and limit debit for OCO scale-out.
    If stop_loss is provided, uses it directly.
    Otherwise defaults from state's active stop or uses stop_multiplier on entry net_credit.
    Returns (stop_trigger_debit, stop_limit_debit).
    """
    try:
        import meic0dte.app.config as app_config
        offset = getattr(app_config, 'LIMIT_OFFSET', 0.20)
    except Exception:
        offset = 0.20

    if stop_loss is not None and float(stop_loss) > 0:
        stop_trigger = round_spx_option_price(float(stop_loss))
        stop_limit = round_spx_option_price(stop_trigger + offset)
        return stop_trigger, stop_limit

    from blocks.stop.stop_math import stop_multiplier_for_state
    stop_mult = stop_multiplier_for_state(state, default=4.0)
    entry = state.get('entry') or {}
    net_cr = float(entry.get('net_credit') or entry.get('limit_credit') or 0)

    stop_trigger = None
    if net_cr > 0:
        stop_trigger = round_spx_option_price(net_cr * stop_mult)
    elif entry.get('two_x_net_credit'):
        stop_trigger = round_spx_option_price(float(entry['two_x_net_credit']))

    if not stop_trigger or stop_trigger <= debit_target:
        active_stop = state.get('active_stop') or {}
        sp = active_stop.get('stop_price') or state.get('designated_stop_price') or active_stop.get('stop_trigger_debit')
        if sp is not None and float(sp) > debit_target:
            stop_trigger = round_spx_option_price(float(sp))
        else:
            stop_trigger = round_spx_option_price(max(debit_target * stop_mult, debit_target + 1.0))

    stop_limit = round_spx_option_price(stop_trigger + offset)
    return stop_trigger, stop_limit


def execute_scale_out_in_monitor(
    monitor,
    close_qty: int,
    target_debit: Optional[float] = None,
    stop_loss: Optional[float] = None,
) -> Dict[str, Any]:
    """Execute scale-out natively inside TradeMonitor thread to avoid cross-process stop conflicts."""
    state = monitor.state
    fpath = monitor.json_path
    broker = monitor.broker
    prices = monitor.prices

    status = str(state.get('status') or '').lower()
    if status != 'open':
        return {'status': 'error', 'error': f'Can only scale out open trades (current status: {status})'}

    holding = int(state.get('filled_quantity') or state.get('quantity') or 0)
    if holding <= 1:
        return {'status': 'error', 'error': f'Cannot scale out: current holding is {holding} contract.'}

    try:
        close_qty = int(close_qty)
    except (TypeError, ValueError):
        return {'status': 'error', 'error': 'Invalid quantity'}

    if close_qty < 1 or close_qty >= holding:
        return {'status': 'error', 'error': f'Quantity to scale out ({close_qty}) must be between 1 and {holding - 1}.'}

    remaining_qty = holding - close_qty
    short_leg = state.get('short_leg') or {}
    long_leg = state.get('long_leg') or {}
    short_sym = short_leg.get('symbol')
    long_sym = long_leg.get('symbol')

    if not short_sym or not long_sym:
        return {'status': 'error', 'error': 'Trade is missing short or long option leg symbol'}

    # 1. Resolve debit limit
    if target_debit is not None and float(target_debit) > 0:
        debit = round_spx_option_price(float(target_debit))
    else:
        quote = resolve_spread_close_debit(state, prices, broker)
        if quote and quote.debit:
            debit = round_spx_option_price(quote.debit)
        else:
            net_cr = float(state.get('entry', {}).get('net_credit', 0) or 0)
            debit = round_spx_option_price(max(net_cr, 0.50))

    # 2. Cancel active stop
    active_stop = state.get('active_stop') or {}
    old_stop_oid = active_stop.get('order_id')
    old_stop_price = active_stop.get('stop_price') or state.get('designated_stop_price')
    old_limit_price = active_stop.get('limit_price')
    old_profit_price = active_stop.get('profit_price')
    old_stop_type = active_stop.get('type') or 'STOP_LIMIT'
    old_phase = active_stop.get('phase', 1)

    if old_stop_price and old_limit_price is None:
        try:
            import meic0dte.app.config as app_config
            offset = getattr(app_config, 'LIMIT_OFFSET', 0.20)
        except Exception:
            offset = 0.20
        old_limit_price = round_spx_option_price(float(old_stop_price) + offset)

    if old_stop_oid:
        cancel_res = broker.cancel_order(str(old_stop_oid))
        if getattr(cancel_res, 'status', '').lower() == 'filled':
            return {
                'status': 'error',
                'error': f'Stop order {old_stop_oid} was filled while scaling out',
            }
        monitor._reregister_alert(str(old_stop_oid), None)
        state_mod.append_stop_history(
            state,
            action='cancelled',
            order_id=str(old_stop_oid),
            price=old_stop_price or old_limit_price,
            phase=old_phase,
            reason=f'scale_out_cancel_{close_qty}_of_{holding}',
            spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
        )

    # 3. Place spread OCO order (take-profit at target debit, stop-loss at spread stop multiple or user specified)
    stop_trigger_debit, stop_limit_debit = calculate_spread_stop_debit(state, debit, stop_loss=stop_loss)
    is_oco = False
    close_res = None

    if hasattr(broker, 'place_spread_oco_order'):
        close_res = broker.place_spread_oco_order(
            short_sym,
            long_sym,
            close_qty,
            profit_debit=debit,
            stop_trigger_debit=stop_trigger_debit,
            stop_limit_debit=stop_limit_debit,
        )
        if close_res.success:
            is_oco = True
        else:
            msg_lower = str(getattr(close_res, 'message', '') or '').lower()
            if 'execute immediately' in msg_lower or 'invalid_oco_price' in msg_lower:
                log.info(
                    'Scale out OCO would execute immediately (%s) — falling back to regular spread close order',
                    close_res.message,
                )
                close_res = broker.place_spread_close_order(short_sym, long_sym, close_qty, debit)

    if close_res is None or (not close_res.success and not is_oco):
        if close_res is None or 'execute immediately' not in str(getattr(close_res, 'message', '')).lower():
            close_res = broker.place_spread_close_order(short_sym, long_sym, close_qty, debit)

    if not close_res.success:
        log.error('Scale out spread close failed in monitor: %s — restoring stop for %d', close_res.message, holding)
        if old_stop_price:
            if old_stop_type == 'OCO' and old_profit_price:
                restored = broker.place_oco_order(
                    short_sym, holding,
                    profit_price=old_profit_price,
                    stop_price=old_stop_price,
                    limit_price=old_limit_price,
                )
            else:
                restored = broker.place_stop_order(
                    short_sym, holding,
                    stop_price=old_stop_price,
                    limit_price=old_limit_price,
                )
            if restored.success:
                state['active_stop'] = {
                    'order_id': str(restored.order_id),
                    'type': old_stop_type,
                    'stop_price': old_stop_price,
                    'limit_price': old_limit_price,
                    'profit_price': old_profit_price,
                    'phase': old_phase,
                    'status': 'working',
                    'placed_at': state_mod.now_iso(),
                    'quantity': holding,
                }
                state['stop_quantity'] = holding
                monitor._reregister_alert(None, str(restored.order_id))
                state_mod.save_state(fpath, state)
        return {'status': 'error', 'error': f'Spread close rejected: {close_res.message}'}

    # 4. Place replacement stop for remaining_qty
    new_stop_oid = None
    if old_stop_price:
        if old_stop_type == 'OCO' and old_profit_price:
            new_stop_res = broker.place_oco_order(
                short_sym, remaining_qty,
                profit_price=old_profit_price,
                stop_price=old_stop_price,
                limit_price=old_limit_price,
            )
        else:
            new_stop_res = broker.place_stop_order(
                short_sym, remaining_qty,
                stop_price=old_stop_price,
                limit_price=old_limit_price,
            )
        if new_stop_res.success:
            new_stop_oid = str(new_stop_res.order_id)
            state['active_stop'] = {
                'order_id': new_stop_oid,
                'type': old_stop_type,
                'stop_price': old_stop_price,
                'limit_price': old_limit_price,
                'profit_price': old_profit_price,
                'phase': old_phase,
                'status': 'working',
                'placed_at': state_mod.now_iso(),
                'quantity': remaining_qty,
            }
            state['stop_quantity'] = remaining_qty
            monitor._reregister_alert(None, new_stop_oid)
            state_mod.append_stop_history(
                state,
                action='placed_scale_out_remaining',
                order_id=new_stop_oid,
                price=old_stop_price,
                phase=old_phase,
                reason=f'scale_out_{close_qty}_closed_remaining_{remaining_qty}',
                spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
            )
        else:
            log.error('Failed to place stop for remaining %d: %s', remaining_qty, new_stop_res.message)
            msg = str(getattr(new_stop_res, 'message', '') or '').lower()
            adopted = False
            if ('already have a closing order' in msg or 'cannot_close_against_more_than_existing_position' in msg) and hasattr(broker, 'find_working_close_order'):
                found = broker.find_working_close_order(short_sym)
                if found and getattr(found, 'order_id', None):
                    new_stop_oid = str(found.order_id)
                    state['active_stop'] = {
                        'order_id': new_stop_oid,
                        'type': old_stop_type,
                        'stop_price': old_stop_price,
                        'limit_price': old_limit_price,
                        'profit_price': old_profit_price,
                        'phase': old_phase,
                        'status': 'working',
                        'placed_at': state_mod.now_iso(),
                        'quantity': remaining_qty,
                    }
                    state['stop_quantity'] = remaining_qty
                    if hasattr(monitor, '_reregister_alert'):
                        monitor._reregister_alert(None, new_stop_oid)
                    adopted = True
            if not adopted:
                state['active_stop'] = None
                state['stop_quantity'] = 0
    else:
        state['active_stop'] = None
        state['stop_quantity'] = 0

    if is_oco:
        state_mod.append_stop_history(
            state,
            action='placed_scale_out_oco',
            order_id=str(close_res.order_id),
            price=stop_trigger_debit,
            phase=old_phase,
            reason=f'scale_out_oco_{close_qty}_contracts_profit_{debit}_stop_{stop_trigger_debit}',
            spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
        )

    actual_debit = debit
    if close_res.filled_price is not None:
        actual_debit = float(close_res.filled_price)
    elif close_res.order_id:
        try:
            time.sleep(0.5)
            status_res = broker.get_order_status(str(close_res.order_id))
            if status_res and status_res.success and status_res.filled_price is not None:
                actual_debit = float(status_res.filled_price)
        except Exception:
            pass

    net_credit = float(state.get('entry', {}).get('net_credit', 0) or 0)
    scale_pnl = round((net_credit - actual_debit) * 100 * close_qty, 2)

    # 5. Create split child trade file for scale-out quantity
    child_lot, child_filename, _ = create_scale_out_child_trade(
        parent_state=state,
        parent_path=fpath,
        close_qty=close_qty,
        close_res=close_res,
        is_oco=is_oco,
        profit_debit=debit,
        stop_trigger_debit=stop_trigger_debit if is_oco else None,
        stop_limit_debit=stop_limit_debit if is_oco else None,
        actual_debit=actual_debit,
    )

    if 'initial_quantity' not in state:
        state['initial_quantity'] = holding
    state['filled_quantity'] = remaining_qty
    state['quantity'] = remaining_qty
    state['holding_quantity'] = remaining_qty
    cum_closed = sum(int(s.get('quantity', 0) or 0) for s in (state.get('scale_outs') or [])) + close_qty
    state['closed_quantity'] = cum_closed

    scale_record = {
        'quantity': close_qty,
        'order_id': str(close_res.order_id) if close_res.order_id else None,
        'order_type': 'SPREAD_OCO' if is_oco else 'SPREAD_LIMIT',
        'debit_limit': debit,
        'profit_target': debit,
        'stop_trigger_debit': stop_trigger_debit if is_oco else None,
        'stop_limit_debit': stop_limit_debit if is_oco else None,
        'stop_loss': stop_trigger_debit if is_oco else None,
        'close_price': actual_debit,
        'short_close_price': getattr(close_res, 'short_fill_price', None),
        'long_close_price': getattr(close_res, 'long_fill_price', None),
        'pnl': scale_pnl,
        'child_lot': child_lot,
        'child_filename': child_filename,
        'status': str(close_res.status or ('working' if is_oco else 'filled')),
        'timestamp': state_mod.now_iso(),
    }
    state.setdefault('scale_outs', []).append(scale_record)
    if remaining_qty <= 0:
        state['status'] = 'closed'
        state['close'] = {
            'reason': 'scale_out_full',
            'timestamp': state_mod.now_iso(),
            'entry_credit': state.get('entry', {}).get('net_credit'),
            'close_price': actual_debit,
            'close_mechanism': 'scale_out',
        }
        state_mod.move_to_closed(fpath, state)
    else:
        state_mod.save_state(fpath, state)

    log.info(
        'Scale out complete in monitor for %s: closed %d @ %.2f (P&L $%.2f), child=%s, remaining %d with stop %s',
        os.path.basename(fpath), close_qty, actual_debit, scale_pnl, child_lot, remaining_qty, new_stop_oid,
    )
    return {
        'status': 'ok',
        'lot': state.get('lot'),
        'child_lot': child_lot,
        'child_filename': child_filename,
        'closed_quantity': close_qty,
        'remaining_quantity': remaining_qty,
        'holding_quantity': remaining_qty,
        'total_closed_quantity': cum_closed,
        'initial_quantity': state.get('initial_quantity'),
        'close_price': actual_debit,
        'profit_target': debit,
        'stop_loss': stop_trigger_debit if is_oco else None,
        'close_order_id': close_res.order_id,
        'order_type': 'SPREAD_OCO' if is_oco else 'SPREAD_LIMIT',
        'stop_trigger_debit': stop_trigger_debit if is_oco else None,
        'new_stop_order_id': new_stop_oid,
        'pnl': scale_pnl,
    }


def scale_out_spread(
    broker,
    prices,
    filename: str,
    close_qty: int,
    debit_limit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    *,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Scale out `close_qty` contracts of an active spread.
    
    When stop_monitor is running, dispatches via command file so monitor executes
    it natively on its own thread, eliminating cross-process race conditions.
    When stop_monitor is not running, executes inline.
    """
    import json
    from common.process_lock import active_lock_pid
    trades_root = root or state_mod._project_root()

    if active_lock_pid('stop_monitor'):
        cmd_dir = os.path.join(trades_root, 'trades', 'commands')
        os.makedirs(cmd_dir, exist_ok=True)
        cmd_path = os.path.join(cmd_dir, f'{filename}.scale_out.json')
        done_path = os.path.join(cmd_dir, f'{filename}.scale_out.done.json')

        if os.path.exists(done_path):
            try:
                os.unlink(done_path)
            except Exception:
                pass

        try:
            with open(cmd_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'action': 'scale_out',
                    'close_qty': int(close_qty),
                    'target_debit': debit_limit,
                    'stop_loss': stop_loss,
                    'ts': state_mod.now_iso(),
                }, f, indent=2)

            deadline = time.time() + 6.0
            while time.time() < deadline:
                time.sleep(0.15)
                if os.path.exists(done_path):
                    try:
                        with open(done_path, 'r', encoding='utf-8') as f:
                            res = json.load(f)
                        os.unlink(done_path)
                        return res
                    except Exception:
                        pass
        except Exception as exc:
            log.warning('Error dispatching scale_out command: %s — falling back to inline', exc)

        log.warning('Timed out or error waiting for stop_monitor scale_out command on %s — executing inline', filename)
        try:
            if os.path.exists(cmd_path):
                os.unlink(cmd_path)
        except Exception:
            pass

    return _scale_out_spread_inline(
        broker, prices, filename, close_qty, debit_limit, stop_loss=stop_loss, root=root,
    )


def _scale_out_spread_inline(
    broker,
    prices,
    filename: str,
    close_qty: int,
    debit_limit: Optional[float] = None,
    stop_loss: Optional[float] = None,
    *,
    root: Optional[str] = None,
) -> Dict[str, Any]:
    if not hasattr(prices, 'get_market_mid'):
        prices = _PricesFeedWrapper(prices)

    fpath = find_active_trade_file(filename, root=root)
    if not fpath:
        return {'status': 'error', 'error': f'Trade file {filename} not found in active trades'}

    state = state_mod.load_state(fpath)
    status = str(state.get('status') or '').lower()
    if status != 'open':
        return {'status': 'error', 'error': f'Can only scale out open trades (current status: {status})'}

    holding = int(state.get('filled_quantity') or state.get('quantity') or 0)
    if holding <= 1:
        return {
            'status': 'error',
            'error': f'Cannot scale out: current holding is {holding} contract. Use Close to exit the entire position.',
        }

    try:
        close_qty = int(close_qty)
    except (TypeError, ValueError):
        return {'status': 'error', 'error': 'Invalid quantity'}

    if close_qty < 1 or close_qty >= holding:
        return {
            'status': 'error',
            'error': f'Quantity to scale out ({close_qty}) must be between 1 and {holding - 1}.',
        }

    remaining_qty = holding - close_qty

    short_leg = state.get('short_leg') or {}
    long_leg = state.get('long_leg') or {}
    short_sym = short_leg.get('symbol')
    long_sym = long_leg.get('symbol')

    if not short_sym or not long_sym:
        return {'status': 'error', 'error': 'Trade is missing short or long option leg symbol'}

    # 1. Resolve debit limit for spread close
    if debit_limit is not None and float(debit_limit) > 0:
        target_debit = round_spx_option_price(float(debit_limit))
    else:
        quote = resolve_spread_close_debit(state, prices, broker)
        if quote and quote.debit:
            target_debit = round_spx_option_price(quote.debit)
        else:
            net_cr = float(state.get('entry', {}).get('net_credit', 0) or 0)
            target_debit = round_spx_option_price(max(net_cr, 0.50))

    # 2. Cancel active stop
    active_stop = state.get('active_stop') or {}
    old_stop_oid = active_stop.get('order_id')
    old_stop_price = active_stop.get('stop_price') or state.get('designated_stop_price')
    old_limit_price = active_stop.get('limit_price')
    old_profit_price = active_stop.get('profit_price')
    old_stop_type = active_stop.get('type') or 'STOP_LIMIT'
    old_phase = active_stop.get('phase', 1)

    if old_stop_price and old_limit_price is None:
        try:
            import meic0dte.app.config as app_config
            offset = getattr(app_config, 'LIMIT_OFFSET', 0.20)
        except Exception:
            offset = 0.20
        old_limit_price = round_spx_option_price(float(old_stop_price) + offset)

    if old_stop_oid:
        cancel_res = broker.cancel_order(str(old_stop_oid))
        if getattr(cancel_res, 'status', '').lower() == 'filled':
            return {
                'status': 'error',
                'error': f'Stop order {old_stop_oid} was filled on broker while scaling out — whole position stopped out.',
            }
        state_mod.append_stop_history(
            state,
            action='cancelled',
            order_id=str(old_stop_oid),
            price=old_stop_price or old_limit_price,
            phase=old_phase,
            reason=f'scale_out_cancel_{close_qty}_of_{holding}',
            spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
        )

    # 3. Place spread OCO order (take-profit at target debit, stop-loss at spread stop multiple or user specified)
    stop_trigger_debit, stop_limit_debit = calculate_spread_stop_debit(state, target_debit, stop_loss=stop_loss)
    is_oco = False
    close_res = None

    if hasattr(broker, 'place_spread_oco_order'):
        close_res = broker.place_spread_oco_order(
            short_sym,
            long_sym,
            close_qty,
            profit_debit=target_debit,
            stop_trigger_debit=stop_trigger_debit,
            stop_limit_debit=stop_limit_debit,
        )
        if close_res.success:
            is_oco = True
        else:
            msg_lower = str(getattr(close_res, 'message', '') or '').lower()
            if 'execute immediately' in msg_lower or 'invalid_oco_price' in msg_lower:
                log.info(
                    'Scale out OCO would execute immediately (%s) — falling back to regular spread close order',
                    close_res.message,
                )
                close_res = broker.place_spread_close_order(short_sym, long_sym, close_qty, target_debit)

    if close_res is None or (not close_res.success and not is_oco):
        if close_res is None or 'execute immediately' not in str(getattr(close_res, 'message', '')).lower():
            close_res = broker.place_spread_close_order(short_sym, long_sym, close_qty, target_debit)

    if not close_res.success:
        log.error('Scale out spread close failed: %s — restoring stop for %d contracts', close_res.message, holding)
        # Restore full stop for original holding
        if old_stop_price:
            if old_stop_type == 'OCO' and old_profit_price:
                restored = broker.place_oco_order(
                    short_sym, holding,
                    profit_price=old_profit_price,
                    stop_price=old_stop_price,
                    limit_price=old_limit_price,
                )
            else:
                restored = broker.place_stop_order(
                    short_sym, holding,
                    stop_price=old_stop_price,
                    limit_price=old_limit_price,
                )
            if restored.success:
                state['active_stop'] = {
                    'order_id': str(restored.order_id),
                    'type': old_stop_type,
                    'stop_price': old_stop_price,
                    'limit_price': old_limit_price,
                    'profit_price': old_profit_price,
                    'phase': old_phase,
                    'status': 'working',
                    'placed_at': state_mod.now_iso(),
                    'quantity': holding,
                }
                state['stop_quantity'] = holding
                state_mod.save_state(fpath, state)
        return {'status': 'error', 'error': f'Spread close rejected: {close_res.message}'}

    # 4. Spread close succeeded -> place replacement stop for remaining_qty
    new_stop_oid = None
    if old_stop_price:
        if old_stop_type == 'OCO' and old_profit_price:
            new_stop_res = broker.place_oco_order(
                short_sym, remaining_qty,
                profit_price=old_profit_price,
                stop_price=old_stop_price,
                limit_price=old_limit_price,
            )
        else:
            new_stop_res = broker.place_stop_order(
                short_sym, remaining_qty,
                stop_price=old_stop_price,
                limit_price=old_limit_price,
            )
        if new_stop_res.success:
            new_stop_oid = str(new_stop_res.order_id)
            state['active_stop'] = {
                'order_id': new_stop_oid,
                'type': old_stop_type,
                'stop_price': old_stop_price,
                'limit_price': old_limit_price,
                'profit_price': old_profit_price,
                'phase': old_phase,
                'status': 'working',
                'placed_at': state_mod.now_iso(),
                'quantity': remaining_qty,
            }
            state['stop_quantity'] = remaining_qty
            state_mod.append_stop_history(
                state,
                action='placed_scale_out_remaining',
                order_id=new_stop_oid,
                price=old_stop_price,
                phase=old_phase,
                reason=f'scale_out_{close_qty}_closed_remaining_{remaining_qty}',
                spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
            )
        else:
            log.error('Failed to place stop for remaining %d contracts: %s', remaining_qty, new_stop_res.message)
            msg = str(getattr(new_stop_res, 'message', '') or '').lower()
            adopted = False
            if ('already have a closing order' in msg or 'cannot_close_against_more_than_existing_position' in msg) and hasattr(broker, 'find_working_close_order'):
                found = broker.find_working_close_order(short_sym)
                if found and getattr(found, 'order_id', None):
                    new_stop_oid = str(found.order_id)
                    state['active_stop'] = {
                        'order_id': new_stop_oid,
                        'type': old_stop_type,
                        'stop_price': old_stop_price,
                        'limit_price': old_limit_price,
                        'profit_price': old_profit_price,
                        'phase': old_phase,
                        'status': 'working',
                        'placed_at': state_mod.now_iso(),
                        'quantity': remaining_qty,
                    }
                    state['stop_quantity'] = remaining_qty
                    adopted = True
            if not adopted:
                state['active_stop'] = None
                state['stop_quantity'] = 0
    else:
        state['active_stop'] = None
        state['stop_quantity'] = 0

    if is_oco:
        state_mod.append_stop_history(
            state,
            action='placed_scale_out_oco',
            order_id=str(close_res.order_id),
            price=stop_trigger_debit,
            phase=old_phase,
            reason=f'scale_out_oco_{close_qty}_contracts_profit_{target_debit}_stop_{stop_trigger_debit}',
            spx_price_at_event=prices.get_spx() if hasattr(prices, 'get_spx') else None,
        )

    # 5. Check if spread close filled immediately or get current fill price
    actual_debit = target_debit
    if close_res.filled_price is not None:
        actual_debit = float(close_res.filled_price)
    elif close_res.order_id:
        # Give broker a moment to report instant fill
        try:
            time.sleep(0.5)
            status_res = broker.get_order_status(str(close_res.order_id))
            if status_res and status_res.success and status_res.filled_price is not None:
                actual_debit = float(status_res.filled_price)
        except Exception:
            pass

    net_credit = float(state.get('entry', {}).get('net_credit', 0) or 0)
    scale_pnl = round((net_credit - actual_debit) * 100 * close_qty, 2)

    # 5. Create split child trade file for scale-out quantity
    child_lot, child_filename, _ = create_scale_out_child_trade(
        parent_state=state,
        parent_path=fpath,
        close_qty=close_qty,
        close_res=close_res,
        is_oco=is_oco,
        profit_debit=target_debit,
        stop_trigger_debit=stop_trigger_debit if is_oco else None,
        stop_limit_debit=stop_limit_debit if is_oco else None,
        actual_debit=actual_debit,
    )

    if 'initial_quantity' not in state:
        state['initial_quantity'] = holding
    state['filled_quantity'] = remaining_qty
    state['quantity'] = remaining_qty
    state['holding_quantity'] = remaining_qty
    cum_closed = sum(int(s.get('quantity', 0) or 0) for s in (state.get('scale_outs') or [])) + close_qty
    state['closed_quantity'] = cum_closed

    scale_record = {
        'quantity': close_qty,
        'order_id': str(close_res.order_id) if close_res.order_id else None,
        'order_type': 'SPREAD_OCO' if is_oco else 'SPREAD_LIMIT',
        'debit_limit': target_debit,
        'profit_target': target_debit,
        'stop_trigger_debit': stop_trigger_debit if is_oco else None,
        'stop_limit_debit': stop_limit_debit if is_oco else None,
        'stop_loss': stop_trigger_debit if is_oco else None,
        'close_price': actual_debit,
        'short_close_price': getattr(close_res, 'short_fill_price', None),
        'long_close_price': getattr(close_res, 'long_fill_price', None),
        'pnl': scale_pnl,
        'child_lot': child_lot,
        'child_filename': child_filename,
        'status': str(close_res.status or ('working' if is_oco else 'filled')),
        'timestamp': state_mod.now_iso(),
    }
    state.setdefault('scale_outs', []).append(scale_record)
    if remaining_qty <= 0:
        state['status'] = 'closed'
        state['close'] = {
            'reason': 'scale_out_full',
            'timestamp': state_mod.now_iso(),
            'entry_credit': state.get('entry', {}).get('net_credit'),
            'close_price': actual_debit,
            'close_mechanism': 'scale_out',
        }
        state_mod.move_to_closed(fpath, state)
    else:
        state_mod.save_state(fpath, state)

    log.info(
        'Scale out complete for %s: closed %d @ %.2f (P&L $%.2f), child=%s, remaining %d with stop order %s',
        filename, close_qty, actual_debit, scale_pnl, child_lot, remaining_qty, new_stop_oid,
    )

    return {
        'status': 'ok',
        'lot': state.get('lot'),
        'child_lot': child_lot,
        'child_filename': child_filename,
        'closed_quantity': close_qty,
        'remaining_quantity': remaining_qty,
        'holding_quantity': remaining_qty,
        'total_closed_quantity': cum_closed,
        'initial_quantity': state.get('initial_quantity'),
        'close_price': actual_debit,
        'profit_target': target_debit,
        'stop_loss': stop_trigger_debit if is_oco else None,
        'close_order_id': close_res.order_id,
        'order_type': 'SPREAD_OCO' if is_oco else 'SPREAD_LIMIT',
        'stop_trigger_debit': stop_trigger_debit if is_oco else None,
        'new_stop_order_id': new_stop_oid,
        'pnl': scale_pnl,
    }
