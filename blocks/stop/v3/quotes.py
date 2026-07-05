"""Manual kill quote resolution (V3 §5.3)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import meic0dte.app.config as app_config
from blocks.stop.v3 import config as v3_config
from common.option_ticks import round_spx_option_price

log = logging.getLogger(__name__)


@dataclass
class QuoteResult:
    debit: float
    source: str
    short_mid: float
    long_mid: float


def resolve_spread_close_debit(
    state: Dict[str, Any],
    prices,
    broker,
) -> Optional[QuoteResult]:
    """
    Price source order: MQTT → broker REST → emergency offset on entry credit.
    Returns None when no source available.
    """
    short_sym = state['short_leg']['symbol']
    long_sym = state['long_leg']['symbol']

    short_p = prices.get_market_mid(short_sym) or prices.get(short_sym)
    long_p = prices.get_market_mid(long_sym) or prices.get(long_sym)
    source = 'mqtt'

    if short_p is None or long_p is None:
        fetch = getattr(broker, 'fetch_option_mids_api', None)
        if fetch:
            try:
                mids = fetch([short_sym, long_sym])
                short_p = short_p if short_p is not None else mids.get(short_sym)
                long_p = long_p if long_p is not None else mids.get(long_sym)
                if short_p is not None and long_p is not None:
                    source = 'broker_rest'
            except Exception as exc:
                log.warning('Broker REST quote fetch failed: %s', exc)

    if short_p is None or long_p is None:
        entry = state.get('entry') or {}
        net_credit = float(entry.get('net_credit') or 0)
        if net_credit > 0:
            emergency = net_credit + v3_config.MANUAL_KILL_EMERGENCY_OFFSET
            short_p = short_p if short_p is not None else emergency + 0.25
            long_p = long_p if long_p is not None else 0.25
            source = 'emergency_offset'
            log.warning(
                'Manual kill emergency quote fallback credit=%.2f offset=%.2f',
                net_credit,
                v3_config.MANUAL_KILL_EMERGENCY_OFFSET,
            )
        else:
            return None

    raw_debit = max(float(short_p) - float(long_p), 0.05)
    debit = round_spx_option_price(raw_debit + app_config.OPEN_PRICE_ADJ)
    return QuoteResult(
        debit=debit,
        source=source,
        short_mid=float(short_p),
        long_mid=float(long_p),
    )
