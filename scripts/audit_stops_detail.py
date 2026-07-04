#!/usr/bin/env python3
"""Detailed stop order status check."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod
from common.broker_factory import get_broker

STOP_IDS = [
    ('11-00_C', '480714929'),
    ('ms-82_C', '480766948'),
    ('ms-83_C', '480779428'),
    ('ms-84_P', '480779430'),
    ('01-15_P', '480814304'),
    ('12-00_C', '480761077'),
    ('12-30_P', '480782243'),
]


def main() -> None:
    broker = get_broker()
    print('=== JSON stop order IDs ===')
    for label, oid in STOP_IDS:
        r = broker.get_order_status(oid)
        print(f'{label} {oid}: success={r.success} status={r.status} msg={r.message or ""}')

    print('\n=== OPEN TRADES active_stop from JSON ===')
    for path in sorted(state_mod.iter_active_trade_paths()):
        st = state_mod.load_state(path)
        if st.get('status') != 'open':
            continue
        lot = st.get('lot')
        side = (st.get('entry') or {}).get('side')
        active = st.get('active_stop') or {}
        print(
            f"{lot}_{side} filled={st.get('filled_quantity')} "
            f"stop={active.get('order_id')} {active.get('stop_price')}/{active.get('limit_price')} "
            f"status={active.get('status')}"
        )

    print('\n=== ALL non-rejected live orders ===')
    live = broker._run(broker.account.get_live_orders(broker.session))
    count = 0
    for o in live:
        st = str(getattr(o, 'status', '')).lower()
        if st in ('rejected', 'cancelled', 'canceled', 'filled', 'expired'):
            continue
        count += 1
        ot = str(getattr(o, 'order_type', '') or getattr(o, 'type', ''))
        leg = (getattr(o, 'legs', None) or [None])[0]
        sym = getattr(leg, 'symbol', '') if leg else ''
        act = getattr(leg, 'action', '') if leg else ''
        print(
            f"  {o.id} {st} {ot} stop={getattr(o,'stop_trigger',None)} "
            f"limit={getattr(o,'price',None)} {act} {sym}"
        )
    if count == 0:
        print('  NONE')


if __name__ == '__main__':
    main()
