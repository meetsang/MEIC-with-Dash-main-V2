"""Executors for Iron Fly: PaperExecutor and BrokerExecutor."""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from brokers.base import BrokerBase, ComboLeg, OrderResult
from strategies.iron_fly.book import IronFly
from strategies.iron_fly.orders import (
    build_iron_fly_close_spec,
    build_iron_fly_open_spec,
    map_broker_status,
    round_credit_down,
    round_debit_up,
)
from strategies.iron_fly.rules import build_closing_legs, build_fly_legs

log = logging.getLogger(__name__)


class BaseExecutor:
    def open_fly(self, plan: Dict[str, Any]) -> OrderResult:
        raise NotImplementedError

    def close_fly(self, fly: IronFly, reason: str = 'cut', market_chain_fn: Optional[Callable] = None) -> OrderResult:
        raise NotImplementedError


class PaperExecutor(BaseExecutor):
    """Simulated local fill book based on live market quotes (§1, §5.5)."""

    def __init__(self, fee_per_leg: float = 1.30):
        self.fee_per_leg = fee_per_leg

    def open_fly(self, plan: Dict[str, Any]) -> OrderResult:
        mid_credit = plan.get('mid_credit', 0.0)
        qty = plan.get('qty', 1)
        oid = f"paper_open_{uuid.uuid4().hex[:8]}"

        fees = round(4 * qty * self.fee_per_leg, 2)
        log.info(
            '[PaperExecutor] OPEN fly center=%s cw=%s pw=%s credit=%.2f qty=%d fees=%.2f id=%s',
            plan.get('center'), plan.get('cw'), plan.get('pw'), mid_credit, qty, fees, oid
        )
        return OrderResult(
            success=True,
            order_id=oid,
            status='filled',
            filled_price=mid_credit,
            filled_price_source='paper_mid',
            filled_quantity=qty,
            order_quantity=qty,
            filled_at=time.time(),
            transmitted=False,
            message='paper_filled_at_mid',
        )

    def close_fly(
        self,
        fly: IronFly,
        reason: str = 'cut',
        market_chain_fn: Optional[Callable] = None,
    ) -> OrderResult:
        qty = fly.qty
        oid = f"paper_close_{uuid.uuid4().hex[:8]}"

        # Exit fills at natural debit (FlowNinja's model §5.5)
        nat_debit = 0.0
        if market_chain_fn:
            try:
                from strategies.iron_fly.rules import price_fly
                chain = market_chain_fn()
                if chain:
                    res = price_fly(chain, fly.center, fly.call_width, fly.put_width)
                    if res:
                        nat_debit = res[2]
            except Exception as exc:
                log.warning('Could not get natural debit for paper close: %s', exc)

        if nat_debit <= 0.0 and fly.mark_debit:
            nat_debit = fly.mark_debit
        if nat_debit <= 0.0 and fly.fill_credit:
            # Fallback estimation if no quote
            nat_debit = fly.fill_credit

        fees = round(4 * qty * self.fee_per_leg, 2) if reason != 'expiry' else 0.0
        log.info(
            '[PaperExecutor] CLOSE fly %s center=%s reason=%s debit=%.2f qty=%d fees=%.2f id=%s',
            fly.id, fly.center, reason, nat_debit, qty, fees, oid
        )
        return OrderResult(
            success=True,
            order_id=oid,
            status='filled',
            filled_price=nat_debit,
            filled_price_source='paper_natural',
            filled_quantity=qty,
            order_quantity=qty,
            filled_at=time.time(),
            transmitted=False,
            message=f'paper_closed_{reason}',
        )


class BrokerExecutor(BaseExecutor):
    """Executes 4-leg iron fly orders via BrokerBase with MEIC chase/cut loops (§5.2, §5.3)."""

    def __init__(
        self,
        broker: BrokerBase,
        entry_wait_s: int = 5,
        entry_max_attempts: int = 6,
        entry_max_s: int = 45,
        max_entry_slip: float = 0.50,
        combo_tick: float = 0.05,
    ):
        self.broker = broker
        self.entry_wait_s = entry_wait_s
        self.entry_max_attempts = entry_max_attempts
        self.entry_max_s = entry_max_s
        self.max_entry_slip = max_entry_slip
        self.combo_tick = combo_tick

    def open_fly(self, plan: Dict[str, Any]) -> OrderResult:
        """Place iron fly using MEIC cancel-and-re-quote loop (§5.2)."""
        expiry = plan['expiry']
        center = plan['center']
        cw = plan['cw']
        pw = plan['pw']
        qty = plan.get('qty', 1)
        base_mid = plan['mid_credit']
        natural_credit = plan.get('natural_credit', base_mid - self.max_entry_slip)

        start_time = time.time()
        attempt = 0

        while attempt < self.entry_max_attempts and (time.time() - start_time) < self.entry_max_s:
            # Limit = mid credit - attempt * 0.05, floor = max(mid - max_entry_slip, natural_credit)
            ideal_limit = base_mid - (attempt * 0.05)
            floor_limit = max(base_mid - self.max_entry_slip, natural_credit)
            limit_credit = max(ideal_limit, floor_limit)
            limit_credit = round_credit_down(limit_credit, self.combo_tick)

            legs = build_fly_legs(expiry, center, cw, pw, qty=qty, broker_format='tastytrade')
            log.info(
                '[BrokerExecutor] Entry attempt %d: center=%s limit=%.2f legs=%d',
                attempt + 1, center, limit_credit, len(legs)
            )

            # Place multi-leg order
            res = self.broker.place_multi_leg_order(
                legs,
                price=limit_credit,
                order_type='LIMIT',
                time_in_force='DAY',
            )

            if not res.success or not res.order_id:
                log.warning('[BrokerExecutor] Entry send failed: %s', res.message)
                return res

            order_id = res.order_id

            # If already filled immediately (e.g. journal or instant match)
            if res.status == 'filled':
                return res

            # Wait entry_wait_s
            time.sleep(self.entry_wait_s)

            # Poll status
            st = self.broker.get_order_status(order_id)
            norm_status = map_broker_status(st.status)

            if norm_status == 'filled':
                log.info('[BrokerExecutor] Order %s FILLED at attempt %d', order_id, attempt + 1)
                return st

            if norm_status == 'rejected':
                log.warning('[BrokerExecutor] Order %s REJECTED: %s', order_id, st.message)
                # Retry once if rejected per §5.2
                if attempt >= 1:
                    return st
                attempt += 1
                continue

            # Not filled -> cancel it (§5.2)
            log.info('[BrokerExecutor] Order %s not filled (%s) -> cancelling', order_id, norm_status)
            c_res = self.broker.cancel_order(order_id)

            # Read status again immediately: cancel can lose race to fill!
            time.sleep(1.0)
            st_after = self.broker.get_order_status(order_id)
            if map_broker_status(st_after.status) == 'filled':
                log.info('[BrokerExecutor] Order %s filled despite cancel', order_id)
                return st_after

            attempt += 1

        return OrderResult(
            success=False,
            order_id=None,
            status='failed',
            message=f'entry_timeout_after_{attempt}_attempts',
        )

    def close_fly(
        self,
        fly: IronFly,
        reason: str = 'cut',
        market_chain_fn: Optional[Callable] = None,
    ) -> OrderResult:
        """Cut iron fly using active replace chase loop (§5.3). Never gives up until cut!"""
        expiry = fly.day.replace('-', '')[2:]  # YYMMDD
        center = fly.center
        cw = fly.call_width
        pw = fly.put_width
        qty = fly.qty

        # Get initial natural debit
        nat_debit = 0.0
        if market_chain_fn:
            try:
                from strategies.iron_fly.rules import price_fly
                chain = market_chain_fn()
                if chain:
                    p_res = price_fly(chain, center, cw, pw)
                    if p_res:
                        nat_debit = p_res[2]
            except Exception:
                pass

        if nat_debit <= 0.0 and fly.fill_credit:
            nat_debit = fly.fill_credit

        attempt = 0
        current_order_id: Optional[str] = None

        while True:
            # Limit debit = natural debit + 0.10 * attempt, up to natural + 1.00
            slip = min(0.10 * (attempt + 1), 1.00)
            limit_debit = round_debit_up(nat_debit + slip, self.combo_tick)

            legs = build_closing_legs(expiry, center, cw, pw, qty=qty, broker_format='tastytrade')

            if current_order_id is None:
                # Initial send
                log.info('[BrokerExecutor] Cut initial send: fly=%s limit=%.2f', fly.id, limit_debit)
                res = self.broker.place_multi_leg_order(
                    legs,
                    price=-limit_debit,  # negative price represents net debit in combo conventions
                    order_type='LIMIT',
                    time_in_force='DAY',
                )
                if not res.success or not res.order_id:
                    log.warning('[BrokerExecutor] Cut order placement failed: %s; retrying in 2s', res.message)
                    time.sleep(2.0)
                    continue
                current_order_id = res.order_id
            else:
                # Replace order (§5.3)
                log.info(
                    '[BrokerExecutor] Cut replace: fly=%s attempt=%d old_id=%s limit=%.2f',
                    fly.id, attempt, current_order_id, limit_debit
                )
                rep_spec = {
                    'order_id': current_order_id,
                    'price': limit_debit,
                    'legs': legs,
                }
                rep_res = self.broker.replace_order(current_order_id, rep_spec)
                if rep_res.success and rep_res.order_id:
                    current_order_id = rep_res.order_id
                else:
                    # Replace failed / answered 400: old order likely filled!
                    st_old = self.broker.get_order_status(current_order_id)
                    if map_broker_status(st_old.status) == 'filled':
                        log.info('[BrokerExecutor] Old order %s filled during replace', current_order_id)
                        return st_old

            # Wait 3 s (§5.3)
            time.sleep(3.0)

            # Check status
            st = self.broker.get_order_status(current_order_id)
            norm = map_broker_status(st.status)
            if norm == 'filled':
                log.info('[BrokerExecutor] Cut order %s FILLED', current_order_id)
                return st

            attempt += 1
            if attempt >= 10:
                log.error('[BrokerExecutor] URGENT: Cut order %s still working after 30s!', current_order_id)
