#!/usr/bin/env python3
"""Audit active trades vs live brokerage stop orders."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod
from blocks.stop.stop_math import exchange_stop_price, stop_multiplier_for_state
from common.broker_factory import get_broker
from common.symbols import symbols_equivalent


def _norm_action(action) -> str:
    return str(action or '').upper().replace(' ', '_').replace('-', '_')


def main() -> int:
    broker = get_broker()
    live = broker._run(broker.account.get_live_orders(broker.session))

    live_stops: dict[str, list[dict]] = {}
    for order in live:
        status = str(getattr(order, 'status', '')).lower()
        if status not in ('live', 'working', 'contingent', 'received', 'open', 'partially filled'):
            continue
        order_type = str(getattr(order, 'order_type', '') or getattr(order, 'type', '')).lower()
        for leg in getattr(order, 'legs', None) or []:
            if _norm_action(getattr(leg, 'action', '')) != 'BUY_TO_CLOSE':
                continue
            sym = str(getattr(leg, 'symbol', ''))
            qty = int(getattr(leg, 'quantity', 0) or getattr(order, 'size', 0) or 0)
            stop_trig = getattr(order, 'stop_trigger', None)
            price = getattr(order, 'price', None)
            live_stops.setdefault(sym, []).append({
                'order_id': str(order.id),
                'status': status,
                'type': order_type,
                'qty': qty,
                'stop': float(stop_trig) if stop_trig is not None else None,
                'limit': abs(float(price)) if price is not None else None,
            })

    ok: list[str] = []
    gaps: list[str] = []
    closed: list[str] = []

    print('=== OPEN TRADES vs BROKER STOPS ===\n')
    for path in sorted(state_mod.iter_active_trade_paths()):
        if 'manual_spread\\trades' in path.replace('/', '\\'):
            continue
        state = state_mod.load_state(path)
        lot = state.get('lot', '?')
        side = (state.get('entry') or {}).get('side', '?')
        status = state.get('status')
        short = state.get('short_leg', {})
        long_leg = state.get('long_leg', {})
        short_sym = short.get('symbol', '')
        filled = int(state.get('filled_quantity') or 0)
        qty = int(state.get('quantity') or 0)
        active = state.get('active_stop') or {}
        json_oid = active.get('order_id')
        strikes = f"{short.get('strike')}/{long_leg.get('strike')}"
        label = f'{lot}_{side} {strikes} x{filled}'

        if status == 'closed':
            closed.append(label)
            continue
        if status != 'open' or filled <= 0:
            gaps.append(f'{label} — status={status} filled={filled}/{qty}')
            continue

        matches: list[dict] = []
        for bsym, orders in live_stops.items():
            if symbols_equivalent(bsym, short_sym):
                matches.extend(orders)

        mult = stop_multiplier_for_state(state)
        calc_stop = exchange_stop_price(float(short.get('fill_price') or 0), mult)

        if matches and any(m['qty'] >= filled for m in matches):
            best = next(m for m in matches if m['qty'] >= filled)
            ok.append(
                f'OK   {label}  broker={best["order_id"]} '
                f'stop/limit={best["stop"]}/{best["limit"]} qty={best["qty"]}  '
                f'json_stop={json_oid or "none"}'
            )
        else:
            gap = f'GAP  {label}  short={short_sym}  calc_stop={calc_stop}  json_stop={json_oid or "NONE"}'
            if matches:
                gap += f'  broker_partial={matches}'
            else:
                gap += '  NO LIVE STOP'
            gaps.append(gap)

    for line in ok:
        print(line)
    print()
    for line in gaps:
        print(line)
    print()
    print(f'Summary: {len(ok)} protected, {len(gaps)} gaps, {len(closed)} closed')
    print()
    print('=== ALL LIVE BROKER ORDERS ===')
    print(f'count: {len(live)}')
    for order in live:
        status = str(getattr(order, 'status', ''))
        order_type = str(getattr(order, 'order_type', '') or getattr(order, 'type', ''))
        legs = []
        for leg in getattr(order, 'legs', None) or []:
            legs.append(
                f'{getattr(leg, "action", "")} {getattr(leg, "symbol", "")} '
                f'x{getattr(leg, "quantity", 0)}'
            )
        stop_trig = getattr(order, 'stop_trigger', None)
        price = getattr(order, 'price', None)
        print(
            f'  {order.id} {status} {order_type} stop={stop_trig} limit={price} | '
            + ' | '.join(legs)
        )
    return 1 if gaps else 0


if __name__ == '__main__':
    raise SystemExit(main())
