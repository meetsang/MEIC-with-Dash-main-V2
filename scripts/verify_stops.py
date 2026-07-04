#!/usr/bin/env python3
"""One-off verify: active JSON stops vs live broker orders."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from brokers.tastytrade_broker import _normalize_leg_action
from common.broker_factory import get_broker
from blocks.stop import state as state_mod

broker = get_broker()
print('=== ACTIVE TRADE JSON vs BROKER ===\n')
json_stops = []
for name in sorted(os.listdir(state_mod.active_dir())):
    if not name.endswith('.json'):
        continue
    path = os.path.join(state_mod.active_dir(), name)
    st = state_mod.load_state(path)
    short = st['short_leg']['symbol']
    active = st.get('active_stop') or {}
    oid = active.get('order_id')
    side = st.get('entry', {}).get('side')
    lot = st.get('lot')
    ss = st['short_leg']['strike']
    ls = st['long_leg']['strike']
    print(name)
    print(f'  {lot} {side}  {ss}/{ls}  status={st.get("status")}')
    print(f'  fills: short={st["short_leg"].get("fill_price")} long={st["long_leg"].get("fill_price")} credit={st["entry"].get("net_credit")}')
    print(f'  JSON stop: {oid} trigger={active.get("stop_price")} limit={active.get("limit_price")} broker_status={active.get("status")}')
    if oid:
        json_stops.append(str(oid))
        r = broker.get_order_status(str(oid))
        raw = r.raw
        trig = getattr(raw, 'stop_trigger', None) if raw else None
        price = getattr(raw, 'price', None) if raw else None
        print(f'  broker poll: status={r.status} type={getattr(raw, "order_type", None) if raw else None} stop={trig} limit={price}')
    print()

print('=== ALL LIVE BUY_TO_CLOSE ORDERS ===\n')
live_ids = []
orders = broker._run(broker.account.get_live_orders(broker.session))
for order in orders or []:
    for leg in getattr(order, 'legs', None) or []:
        if _normalize_leg_action(getattr(leg, 'action', '')) != 'BUY_TO_CLOSE':
            continue
        live_ids.append(str(order.id))
        print(f'  {order.id}  {order.status}  {order.order_type}  {leg.symbol}')
        print(f'    stop_trigger={getattr(order, "stop_trigger", None)}  limit={order.price}')

print(f'\nSummary: {len(json_stops)} stops in JSON, {len(live_ids)} live BUY_TO_CLOSE at broker')
json_set = set(json_stops)
live_set = set(live_ids)
if json_set == live_set:
    print('MATCH: JSON stop order IDs == live broker stop orders')
else:
    print(f'  only in JSON: {sorted(json_set - live_set)}')
    print(f'  only at broker: {sorted(live_set - json_set)}')
