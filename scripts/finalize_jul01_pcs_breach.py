#!/usr/bin/env python3
"""Archive Jul 1 PCS breach closes with operator-reconciled long fills."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from blocks.session.plan import SessionPlan
from blocks.stop import state as state_mod

CLOSES = [
    {
        'path': os.path.join(
            ROOT, 'trades', 'active', 'MEIC_IC', '12-30_P_20260701T122901.json',
        ),
        'slot_key': '12-30_P',
        'short_close_price': 2.65,
        'long_close_price': 0.65,
        'long_close_limit_price': 0.65,
    },
    {
        'path': os.path.join(
            ROOT, 'trades', 'active', 'MEIC_IC', '01-15_P_20260701T131400.json',
        ),
        'slot_key': '01-15_P',
        'short_close_price': 3.30,
        'long_close_price': 0.30,
        'long_close_limit_price': 0.30,
    },
]


def _finalize(item: dict) -> str:
    path = item['path']
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    state = state_mod.load_state(path)
    entry_credit = state_mod.section(state, 'entry').get('net_credit')
    short_fill = state_mod.section(state, 'short_leg').get('fill_price')
    long_fill = state_mod.section(state, 'long_leg').get('fill_price')
    short_close = float(item['short_close_price'])
    long_close = float(item['long_close_price'])
    long_limit = float(item['long_close_limit_price'])

    state['status'] = 'closed'
    state['close_mechanism'] = 'exchange_stop'
    state['active_stop'] = None
    state['long_close_order_id'] = None
    state['short_close_price'] = short_close
    state['long_close_price'] = long_close
    state['long_close_limit_price'] = long_limit
    state['short_closed_at'] = state.get('short_closed_at')
    state['close'] = {
        'reason': 'exchange_stop',
        'timestamp': state_mod.now_iso(),
        'entry_credit': entry_credit,
        'short_fill': short_fill,
        'long_fill': long_fill,
        'short_close_price': short_close,
        'long_close_price': long_close,
        'short_close_limit_price': state.get('short_close_limit_price'),
        'long_close_limit_price': long_limit,
        'close_mechanism': 'exchange_stop',
        'spx_at_close': None,
        'operator_reconciled': True,
        'operator_note': 'Long closed manually on Tasty after MQTT SPX-fallback bug (Jul 1).',
    }
    archived = state_mod.move_to_closed(path, state)
    return archived


def main() -> int:
    session_path = os.path.join(ROOT, 'trades', 'session', 'MEIC_IC_2026-07-01.csv')
    plan = SessionPlan.load(session_path)

    for item in CLOSES:
        archived = _finalize(item)
        plan.update_row(item['slot_key'], state='closed', trade_path=archived)
        print(f"OK {item['slot_key']} -> {archived}")

    plan.save()
    print('Session CSV updated.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
