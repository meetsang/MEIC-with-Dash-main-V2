#!/usr/bin/env python3
"""
Fetch one open/filled spread order from TastyTrade via internal broker code
and print everything needed to repair trades/active JSON (leg fill prices, etc.).

Usage (from MEIC-with-Dash-main):
    .venv\\Scripts\\python.exe scripts\\inspect_open_order.py
    .venv\\Scripts\\python.exe scripts\\inspect_open_order.py --order-id 477705364
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from common.broker_factory import get_broker
from blocks.stop import state as state_mod
from blocks.stop.fill_sync import apply_order_result_to_state, sync_open_order


def _serialize(obj: Any, depth: int = 0) -> Any:
    """Best-effort JSON-safe dump of tastytrade SDK objects."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if depth > 6:
        return repr(obj)
    if isinstance(obj, (list, tuple)):
        return [_serialize(x, depth + 1) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _serialize(v, depth + 1) for k, v in obj.items()}
    if hasattr(obj, 'model_dump'):
        try:
            return obj.model_dump()
        except Exception:
            pass
    if hasattr(obj, '__dict__'):
        out: Dict[str, Any] = {}
        for k, v in vars(obj).items():
            if k.startswith('_'):
                continue
            out[k] = _serialize(v, depth + 1)
        return out
    return repr(obj)


def _dump_leg(leg: Any, index: int) -> None:
    print(f'\n  --- leg[{index}] ---')
    print(f'  action:              {getattr(leg, "action", None)}')
    print(f'  symbol:              {getattr(leg, "symbol", None)}')
    print(f'  quantity:            {getattr(leg, "quantity", None)}')
    print(f'  remaining_quantity:  {getattr(leg, "remaining_quantity", None)}')
    fills = getattr(leg, 'fills', None) or []
    print(f'  fills count:         {len(fills)}')
    if not fills:
        print('  fills:               (empty — adapter cannot set leg fill_price)')
    for i, fill in enumerate(fills):
        print(f'    fill[{i}].quantity:    {getattr(fill, "quantity", None)}')
        print(f'    fill[{i}].fill_price:  {getattr(fill, "fill_price", None)}')
        print(f'    fill[{i}].filled_at:   {getattr(fill, "filled_at", None)}')
        print(f'    fill[{i}].fill_id:     {getattr(fill, "fill_id", None)}')


def _find_trade_json(order_id: str) -> Optional[str]:
    active = state_mod.active_dir()
    needle = str(order_id)
    for name in os.listdir(active):
        if not name.endswith('.json'):
            continue
        path = os.path.join(active, name)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if str(data.get('open_order_id')) == needle:
                return path
        except Exception:
            continue
    return None


def _print_order_result(label: str, result) -> None:
    print(f'\n{"=" * 60}')
    print(label)
    print('=' * 60)
    print(f'  success:           {result.success}')
    print(f'  order_id:          {result.order_id}')
    print(f'  status:            {result.status}')
    print(f'  order_quantity:    {result.order_quantity}')
    print(f'  filled_quantity:   {result.filled_quantity}')
    print(f'  remaining_quantity:{result.remaining_quantity}')
    print(f'  filled_price:      {result.filled_price}  (spread credit if both legs known)')
    print(f'  short_fill_price:  {result.short_fill_price}')
    print(f'  long_fill_price:   {result.long_fill_price}')
    if result.message:
        print(f'  message:           {result.message}')

    order = result.raw
    if order is None:
        print('\n  raw order:           (none)')
        return

    print('\n  --- raw PlacedOrder ---')
    print(f'  id:                {getattr(order, "id", None)}')
    print(f'  status:            {getattr(order, "status", None)}')
    print(f'  size:              {getattr(order, "size", None)}')
    print(f'  price:             {getattr(order, "price", None)}')
    print(f'  order_type:        {getattr(order, "order_type", None)}')
    print(f'  time_in_force:     {getattr(order, "time_in_force", None)}')
    print(f'  received_at:       {getattr(order, "received_at", None)}')
    print(f'  updated_at:        {getattr(order, "updated_at", None)}')

    legs = getattr(order, 'legs', None) or []
    print(f'\n  legs:              {len(legs)}')
    for i, leg in enumerate(legs):
        _dump_leg(leg, i)


def _print_json_repair(state: dict, result) -> None:
    print(f'\n{"=" * 60}')
    print('TRADE JSON — before apply_order_result_to_state')
    print('=' * 60)
    print(f'  status:            {state.get("status")}')
    print(f'  filled_quantity:   {state.get("filled_quantity")}')
    print(f'  fully_filled:      {state.get("open_order", {}).get("fully_filled")}')
    print(f'  short fill_price:  {state.get("short_leg", {}).get("fill_price")}')
    print(f'  long fill_price:   {state.get("long_leg", {}).get("fill_price")}')

    snapshot = json.loads(json.dumps(state))
    changed = apply_order_result_to_state(snapshot, result)
    print(f'\n{"=" * 60}')
    print(f'TRADE JSON — after apply_order_result_to_state (changed={changed})')
    print('=' * 60)
    print(f'  status:            {snapshot.get("status")}')
    print(f'  filled_quantity:   {snapshot.get("filled_quantity")}')
    print(f'  fully_filled:      {snapshot.get("open_order", {}).get("fully_filled")}')
    print(f'  net_credit:        {snapshot.get("entry", {}).get("net_credit")}')
    print(f'  two_x_net_credit:  {snapshot.get("entry", {}).get("two_x_net_credit")}')
    print(f'  short fill_price:  {snapshot.get("short_leg", {}).get("fill_price")}')
    print(f'  long fill_price:   {snapshot.get("long_leg", {}).get("fill_price")}')
    print(f'  two_x_short:       {snapshot.get("short_leg", {}).get("two_x_short")}')

    if snapshot.get('status') == 'open':
        short = float(snapshot['short_leg']['fill_price'])
        stop_mult = 2.0
        stop_price = round(round(((short - 0.10) * stop_mult) / 0.05) * 0.05, 2)
        print(f'\n  computed initial stop trigger (2x short): ~{stop_price}')
    else:
        print('\n  status would stay pending_fill — leg fill prices still missing')


def main() -> int:
    parser = argparse.ArgumentParser(description='Inspect TastyTrade open order fill details')
    parser.add_argument('--order-id', default='477705364', help='TastyTrade order id')
    parser.add_argument('--paper', action='store_true', help='Use paper session')
    parser.add_argument('--dump-raw', action='store_true', help='Print full raw order JSON')
    args = parser.parse_args()

    order_id = str(args.order_id).strip()
    print(f'Inspecting order {order_id} (project root: {ROOT})')

    json_path = _find_trade_json(order_id)
    if json_path:
        print(f'Found trades/active JSON: {json_path}')
    else:
        print('No matching trades/active JSON found (broker query will still run).')

    print('\nConnecting broker...')
    broker = get_broker(paper=args.paper)

    result = broker.get_order_status(order_id)
    _print_order_result('broker.get_order_status()', result)

    if not result.success:
        print('\nOrder lookup failed.')
        return 1

    # Also try live-orders list vs get_order path (same parser, but log source)
    try:
        live = broker._run(broker.account.get_live_orders(broker.session))
        in_live = any(str(o.id) == order_id for o in (live or []))
        print(f'\nOrder in live book: {in_live}')
        if not in_live:
            print('(Filled orders are fetched via account.get_order — expected for this case.)')
    except Exception as exc:
        print(f'\nCould not list live orders: {exc}')

    if args.dump_raw and result.raw is not None:
        print(f'\n{"=" * 60}')
        print('RAW ORDER (serialized)')
        print('=' * 60)
        print(json.dumps(_serialize(result.raw), indent=2, default=str))

    if json_path:
        state = state_mod.load_state(json_path)
        _print_json_repair(state, result)

        print(f'\n{"=" * 60}')
        print('sync_open_order simulation (force=True, fully_filled gate)')
        print('=' * 60)
        sim = state_mod.load_state(json_path)
        gate_blocked = state_mod.section(sim, 'open_order').get('fully_filled')
        print(f'  fully_filled gate blocks sync: {bool(gate_blocked)}')
        if gate_blocked:
            sim['open_order']['fully_filled'] = False
            print('  (cleared fully_filled for simulation only)')
        changed, sync_result = sync_open_order(sim, broker, force=True)
        print(f'  sync changed state: {changed}')
        if sync_result:
            print(f'  sync short_fill_price: {sync_result.short_fill_price}')
            print(f'  sync long_fill_price:  {sync_result.long_fill_price}')
        print(f'  sim status after sync: {sim.get("status")}')
        print(f'  sim short fill_price:  {sim.get("short_leg", {}).get("fill_price")}')
        print(f'  sim long fill_price:   {sim.get("long_leg", {}).get("fill_price")}')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
