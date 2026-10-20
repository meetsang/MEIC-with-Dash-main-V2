"""Execution layer for XSP Iron Fly strategy (Paper and Live Broker)."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from brokers.base import BrokerBase, ComboLeg
from strategies.iron_fly.book import FlyStatus, IronFly
from strategies.iron_fly_xsp.book import IronFlyXspPair
from strategies.iron_fly_xsp.orders import build_closing_legs_xsp, build_fly_legs_xsp, round_credit_down, round_debit_up
from strategies.iron_fly_xsp.rules import price_fly_xsp

log = logging.getLogger(__name__)

FEE_PER_LEG_CONTRACT = 1.30


class PaperExecutorXsp:
    """Simulates realistic paper executions and fees for XSP Iron Fly pairs."""

    def __init__(self, broker: Optional[BrokerBase] = None):
        self.broker = broker

    def execute_open_pair(
        self,
        pair: IronFlyXspPair,
        expiry_yymmdd: str,
        chain: Dict[Tuple[str, int], dict],
    ) -> bool:
        """
        Executes paper entry for both flies in the pair.
        Fills at mid credit, charges 4 legs * $1.30 * quantity per fly.
        """
        for fly in (pair.fly_a, pair.fly_b):
            pricing = price_fly_xsp(fly.center, fly.cw, fly.pw, chain)
            if not pricing:
                fly.status = FlyStatus.FAILED
                pair.status = FlyStatus.FAILED
                log.warning('PaperExecutorXsp: Failed to price fly %s at center %d', fly.id, fly.center)
                return False
            nat, mid = pricing
            credit = round_credit_down(mid)
            if credit <= 0:
                fly.status = FlyStatus.FAILED
                pair.status = FlyStatus.FAILED
                log.warning('PaperExecutorXsp: Non-positive credit %.2f for %s', credit, fly.id)
                return False

            fly.credit = credit
            fly.be_low = round(fly.center - credit, 2)
            fly.be_high = round(fly.center + credit, 2)
            fly.opened_at = datetime.now().isoformat()
            fly.entry_fill_time = fly.opened_at
            fly.fees_paid = round(4 * FEE_PER_LEG_CONTRACT * fly.quantity, 2)
            fly.status = FlyStatus.OPEN
            fly.legs = build_fly_legs_xsp(fly.center, fly.cw, fly.pw, expiry_yymmdd, fly.quantity)

        pair.opened_at = datetime.now().isoformat()
        pair.status = FlyStatus.OPEN
        log.info(
            'PaperExecutorXsp: Filled pair %s at avg credit $%.2f (A: $%.2f, B: $%.2f)',
            pair.pair_id, pair.credit_avg, pair.fly_a.credit, pair.fly_b.credit,
        )
        return True

    def execute_close_pair(
        self,
        pair: IronFlyXspPair,
        expiry_yymmdd: str,
        chain: Dict[Tuple[str, int], dict],
        reason: str = 'SPOT_CUT',
    ) -> bool:
        """
        Simulates natural debit market close for both flies in the pair.
        Charges 4 legs * $1.30 exit fee per fly.
        """
        pair.status = FlyStatus.CLOSING
        for fly in (pair.fly_a, pair.fly_b):
            fly.status = FlyStatus.CLOSING
            pricing = price_fly_xsp(fly.center, fly.cw, fly.pw, chain)
            debit = 0.0
            if pricing:
                nat, mid = pricing
                # Closing debit at natural market debit (approx width - credit)
                debit = round_debit_up(max(0.05, max(fly.cw, fly.pw) - nat))
            else:
                debit = round_debit_up(max(0.05, max(fly.cw, fly.pw) - (fly.credit or 0.0)))

            fly.exit_debit = debit
            now_close = datetime.now().isoformat()
            fly.closed_at = now_close
            fly.exit_fill_time = now_close
            fly.close_reason = reason
            exit_fee = round(4 * FEE_PER_LEG_CONTRACT * fly.quantity, 2)
            fly.fees_paid += exit_fee
            gross_pnl = ((fly.credit or 0.0) - debit) * 100 * fly.quantity
            fly.realized_pnl = round(gross_pnl - fly.fees_paid, 2)
            fly.unrealized_pnl = 0.0
            fly.status = FlyStatus.CLOSED

        pair.closed_at = datetime.now().isoformat()
        pair.status = FlyStatus.CLOSED
        log.info(
            'PaperExecutorXsp: Closed pair %s (Reason: %s, Realized P/L: $%.2f)',
            pair.pair_id, reason, pair.realized_pnl,
        )
        return True


class BrokerExecutorXsp:
    """Live broker order submission and combo chase loops for XSP."""

    def __init__(self, broker: BrokerBase):
        self.broker = broker

    def execute_open_pair(
        self,
        pair: IronFlyXspPair,
        expiry_yymmdd: str,
        chain: Dict[Tuple[str, int], dict],
    ) -> bool:
        """
        Submits 4-leg combo Net Credit orders for both flies in the pair.
        """
        success = True
        for fly in (pair.fly_a, pair.fly_b):
            pricing = price_fly_xsp(fly.center, fly.cw, fly.pw, chain)
            if not pricing:
                fly.status = FlyStatus.FAILED
                success = False
                continue
            nat, mid = pricing
            target_credit = round_credit_down(mid)
            legs = build_fly_legs_xsp(fly.center, fly.cw, fly.pw, expiry_yymmdd, fly.quantity)
            fly.legs = legs

            try:
                order = self.broker.place_combo_order(
                    legs=legs,
                    price=target_credit,
                    action_type='NET_CREDIT',
                )
                fly.status = FlyStatus.OPEN
                fly.credit = target_credit
                fly.be_low = round(fly.center - target_credit, 2)
                fly.be_high = round(fly.center + target_credit, 2)
                now_open = datetime.now().isoformat()
                fly.opened_at = now_open
                fly.entry_fill_time = now_open
                fly.fees_paid = round(4 * FEE_PER_LEG_CONTRACT * fly.quantity, 2)
            except Exception as exc:
                log.error('Broker order failed for XSP fly %s: %s', fly.id, exc)
                fly.status = FlyStatus.FAILED
                success = False

        pair.opened_at = datetime.now().isoformat()
        pair.status = FlyStatus.OPEN if success else FlyStatus.FAILED
        return success

    def execute_close_pair(
        self,
        pair: IronFlyXspPair,
        expiry_yymmdd: str,
        chain: Dict[Tuple[str, int], dict],
        reason: str = 'SPOT_CUT',
    ) -> bool:
        """
        Submits 4-leg Net Debit combo closing orders for both flies in the pair.
        """
        for fly in (pair.fly_a, pair.fly_b):
            legs = build_closing_legs_xsp(fly.center, fly.cw, fly.pw, expiry_yymmdd, fly.quantity)
            pricing = price_fly_xsp(fly.center, fly.cw, fly.pw, chain)
            debit = max(fly.cw, fly.pw)
            if pricing:
                debit = round_debit_up(max(0.05, max(fly.cw, fly.pw) - pricing[0]))
            try:
                self.broker.place_combo_order(
                    legs=legs,
                    price=debit,
                    action_type='NET_DEBIT',
                )
                fly.exit_debit = debit
                now_close = datetime.now().isoformat()
                fly.closed_at = now_close
                fly.exit_fill_time = now_close
                fly.close_reason = reason
                fly.fees_paid += round(4 * FEE_PER_LEG_CONTRACT * fly.quantity, 2)
                fly.realized_pnl = round(((fly.credit or 0.0) - debit) * 100 * fly.quantity - fly.fees_paid, 2)
                fly.unrealized_pnl = 0.0
                fly.status = FlyStatus.CLOSED
            except Exception as exc:
                log.error('Broker close failed for XSP fly %s: %s', fly.id, exc)
                fly.status = FlyStatus.FAILED

        pair.closed_at = datetime.now().isoformat()
        pair.status = FlyStatus.CLOSED
        return True
