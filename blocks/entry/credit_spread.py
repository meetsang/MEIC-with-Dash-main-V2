"""Scan and open credit spreads (PCS / CCS)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from blocks.entry.config import CreditEntryConfig
from blocks.entry.spread_scan import SpreadCandidate, resolve_scan_otm_max, scan_credit_spreads


class CreditSpreadEntry:
    """Strategy-facing entry block for credit vertical spreads."""

    def __init__(
        self,
        broker,
        config: Optional[CreditEntryConfig] = None,
        log: Optional[logging.Logger] = None,
    ):
        self.broker = broker
        self.config = config or CreditEntryConfig.from_meic_config()
        self.log = log or logging.getLogger('credit_spread_entry')

    def scan_for_meic(
        self,
        side: str,
        expiry_yymmdd: str,
        lot: str,
    ) -> List[SpreadCandidate]:
        """MEIC scheduled entry — width band + credit min/max, first match."""
        side = side.upper()
        otm_max = resolve_scan_otm_max(credit_min=self.config.credit_min)
        return scan_credit_spreads(
            self.broker,
            side,
            expiry_yymmdd,
            lot,
            self.log,
            spread_width_min=self.config.spread_width_min,
            spread_width_max=self.config.spread_width_max,
            otm_min=self.config.otm_min,
            otm_max=otm_max,
            credit_min=self.config.credit_min,
            credit_max=self.config.credit_max_for_side(side),
            max_results=1,
            quote_source=self.config.quote_source,
        )

    def scan_for_target(
        self,
        side: str,
        expiry_yymmdd: str,
        lot: str,
        *,
        spread_width: int,
        target_credit: float,
        max_results: int = 3,
        otm_max: int | None = None,
    ) -> List[SpreadCandidate]:
        """Manual spread — fixed width, rank by distance to target credit."""
        if otm_max is None:
            otm_max = resolve_scan_otm_max(target_credit=target_credit)
        return scan_credit_spreads(
            self.broker,
            side.upper(),
            expiry_yymmdd,
            lot,
            self.log,
            spread_width=spread_width,
            otm_min=self.config.otm_min,
            otm_max=otm_max if otm_max is not None else self.config.otm_max,
            target_credit=target_credit,
            min_market_credit=self.config.min_market_credit,
            max_results=max_results,
            quote_source=self.config.quote_source,
        )

    def open_candidate(
        self,
        candidate: SpreadCandidate,
        side: str,
        quantity: int,
        lot: str,
    ) -> Tuple[str, str, str, float, int, int]:
        """Place NET_CREDIT order for a scan candidate. Returns leg info + order_id."""
        result = self.broker.place_spread_order(
            candidate.short_symbol,
            candidate.long_symbol,
            quantity,
            candidate.market_credit,
        )
        if not result.success:
            raise RuntimeError(f'Open order failed: {result.message}')
        self.log.info(
            '%s %s spread placed credit=%.2f order=%s',
            lot,
            side,
            candidate.market_credit,
            result.order_id,
        )
        return (
            candidate.short_symbol,
            candidate.long_symbol,
            result.order_id,
            candidate.market_credit,
            candidate.short_strike,
            candidate.long_strike,
        )

    def write_handshake(
        self,
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
    ) -> str:
        from blocks.entry.handshake import write_credit_spread_handshake

        return write_credit_spread_handshake(
            lot=lot,
            side=side,
            short_symbol=short_symbol,
            long_symbol=long_symbol,
            short_strike=short_strike,
            long_strike=long_strike,
            quantity=quantity,
            open_order_id=open_order_id,
            limit_credit=limit_credit,
            strategy=strategy,
            active_directory=active_directory,
        )
