#!/usr/bin/env python3
"""Verify stops via per-order broker lookup (reliable for TT stop limits)."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, ROOT)

from blocks.stop import state as state_mod
from blocks.stop.stop_math import exchange_stop_price, stop_multiplier_for_state
from common.broker_factory import get_broker

LIVE_STOP = frozenset({'live', 'working', 'contingent', 'received', 'open', 'partially filled'})


def main() -> None:
    broker = get_broker()
    ok = []
    gaps = []

    print('=== STOP AUDIT (per-order lookup) ===\n')
    for path in sorted(state_mod.iter_active_trade_paths()):
        if 'manual_spread\\trades' in path.replace('/', '\\'):
            continue
        st = state_mod.load_state(path)
        lot = st.get('lot', '?')
        side = (st.get('entry') or {}).get('side', '?')
        status = st.get('status')
        if status == 'closed':
            continue
        if status != 'open':
            gaps.append(f'{lot}_{side} — status={status}')
            continue

        short = st.get('short_leg', {})
        long_leg = st.get('long_leg', {})
        filled = int(st.get('filled_quantity') or 0)
        strikes = f"{short.get('strike')}/{long_leg.get('strike')}"
        label = f'{lot}_{side} {strikes} x{filled}'
        active = st.get('active_stop') or {}
        oid = active.get('order_id')

        if not oid:
            calc = exchange_stop_price(
                float(short.get('fill_price') or 0),
                stop_multiplier_for_state(st),
            )
            gaps.append(f'GAP  {label} — NO stop in JSON (calc={calc})')
            continue

        result = broker.get_order_status(str(oid))
        st_low = str(result.status or '').lower()
        if result.success and st_low in LIVE_STOP:
            ok.append(
                f'OK   {label} — stop {oid} @ '
                f'{active.get("stop_price")}/{active.get("limit_price")} ({st_low})'
            )
        else:
            gaps.append(
                f'GAP  {label} — stop {oid} broker_status={result.status} '
                f'msg={result.message or ""}'
            )

    for line in ok:
        print(line)
    print()
    for line in gaps:
        print(line)
    print(f'\nSummary: {len(ok)} OK, {len(gaps)} gaps')


if __name__ == '__main__':
    main()
