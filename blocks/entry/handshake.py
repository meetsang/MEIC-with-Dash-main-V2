"""Write pending-fill handshake JSON for credit spread entry."""
from __future__ import annotations

import os

from common.symbols import to_tastytrade
from blocks.stop import state as state_mod


def write_credit_spread_handshake(
    *,
    lot: str,
    side: str,
    short_symbol: str,
    long_symbol: str,
    short_strike: int,
    long_strike: int,
    quantity: int,
    open_order_id: str,
    limit_credit: float,
    strategy: str = 'MEIC_IC',
    active_directory: str | None = None,
    existing_path: str | None = None,
    entry_ts: str | None = None,
    reason: str = 'placed',
    on_unfilled_step: str | None = None,
) -> str:
    state_mod.ensure_dirs()
    short_tt = to_tastytrade(short_symbol)
    long_tt = to_tastytrade(long_symbol)

    if existing_path and os.path.isfile(existing_path):
        state = state_mod.load_state(existing_path)
        state_mod.update_pending_order(
            state,
            open_order_id=open_order_id,
            limit_credit=limit_credit,
            short_symbol=short_tt,
            long_symbol=long_tt,
            short_strike=short_strike,
            long_strike=long_strike,
            target_quantity=quantity,
        )
        state_mod.append_order_history(
            state,
            order_id=open_order_id,
            limit_credit=limit_credit,
            short_strike=short_strike,
            long_strike=long_strike,
            status='working',
            filled_quantity=0,
            reason=reason,
            on_unfilled_step=on_unfilled_step,
        )
        state_mod.save_state(existing_path, state)
        return existing_path

    dest_dir = active_directory or state_mod.active_dir()
    os.makedirs(dest_dir, exist_ok=True)
    filename = state_mod.stable_trade_filename(lot, side, entry_ts)
    path = os.path.join(dest_dir, filename)

    state = state_mod.create_pending_state(
        strategy=strategy,
        lot=lot,
        side=side,
        short_symbol=short_tt,
        long_symbol=long_tt,
        short_strike=short_strike,
        long_strike=long_strike,
        target_quantity=quantity,
        open_order_id=open_order_id,
        limit_credit=limit_credit,
    )
    state_mod.append_order_history(
        state,
        order_id=open_order_id,
        limit_credit=limit_credit,
        short_strike=short_strike,
        long_strike=long_strike,
        status='working',
        filled_quantity=0,
        reason=reason,
        on_unfilled_step=on_unfilled_step,
    )
    state_mod.save_state(path, state)
    return path
