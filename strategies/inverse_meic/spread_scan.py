"""Debit spread scanning and candidate selection for Inverse MEIC.

Selects 25-wide SPX vertical debit spreads targeting ~$0.80 - $1.10 debit per spread:
- Call Debit Spread (CDS): BUY lower Call (closer to money), SELL higher Call (OTM).
- Put Debit Spread (PDS): BUY higher Put (closer to money), SELL lower Put (OTM).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from common.symbols import build_tastytrade_symbol, to_tastytrade

log = logging.getLogger('inverse_meic_scan')


@dataclass
class DebitSpreadCandidate:
    side: str  # 'C' | 'P'
    long_symbol: str
    short_symbol: str
    long_strike: int
    short_strike: int
    width: int
    market_debit: float
    long_mid: float
    short_mid: float
    distance_from_target: float = 0.0


def scan_debit_spread(
    broker,
    side: str,
    expiry_yymmdd: str,
    spx_price: float,
    *,
    width: int = 25,
    target_debit: float = 0.90,
    max_debit: float = 1.10,
    min_debit: float = 0.60,
    otm_min: int = 10,
    otm_max: int = 200,
    step: int = 5,
    logger: Optional[logging.Logger] = None,
) -> Optional[DebitSpreadCandidate]:
    """Scan the SPX options chain for the vertical debit spread closest to target_debit."""
    side = side.upper()
    lg = logger or log

    # Generate candidate strike pairs
    pairs: List[Tuple[int, int, str, str]] = []
    # Round spx_price to nearest 5
    spx_rounded = int(round(spx_price / 5.0) * 5)

    for otm in range(otm_min, otm_max + step, step):
        if side == 'C':
            long_strike = spx_rounded + otm
            short_strike = long_strike + width
        else:
            long_strike = spx_rounded - otm
            short_strike = long_strike - width

        long_sym = build_tastytrade_symbol(expiry_yymmdd, side, long_strike)
        short_sym = build_tastytrade_symbol(expiry_yymmdd, side, short_strike)
        pairs.append((long_strike, short_strike, long_sym, short_sym))

    # Collect symbols to quote
    all_syms: List[str] = []
    for _, _, l_sym, s_sym in pairs:
        all_syms.extend([l_sym, s_sym])

    # Fetch quotes via broker REST API or pricing cache
    price_map: Dict[str, float] = {}
    if broker is not None and hasattr(broker, 'fetch_option_mids_api'):
        try:
            # Batch fetch in chunks of 50
            for i in range(0, len(all_syms), 50):
                batch = all_syms[i:i + 50]
                mids = broker.fetch_option_mids_api(batch)
                if mids:
                    price_map.update(mids)
        except Exception as exc:
            lg.warning('Broker fetch_option_mids_api error: %s', exc)

    # Evaluate candidates
    candidates: List[DebitSpreadCandidate] = []
    for long_k, short_k, l_sym, s_sym in pairs:
        l_mid = price_map.get(to_tastytrade(l_sym))
        s_mid = price_map.get(to_tastytrade(s_sym))

        if l_mid is None or s_mid is None:
            continue

        raw_debit = round(float(l_mid) - float(s_mid), 2)
        if min_debit <= raw_debit <= max_debit:
            candidates.append(
                DebitSpreadCandidate(
                    side=side,
                    long_symbol=l_sym,
                    short_symbol=s_sym,
                    long_strike=long_k,
                    short_strike=short_k,
                    width=width,
                    market_debit=raw_debit,
                    long_mid=float(l_mid),
                    short_mid=float(s_mid),
                    distance_from_target=abs(raw_debit - target_debit),
                )
            )

    if candidates:
        candidates.sort(key=lambda c: c.distance_from_target)
        best = candidates[0]
        lg.info(
            'Found %s debit spread: Buy %s Sell %s debit=%.2f (target=%.2f)',
            side, best.long_strike, best.short_strike, best.market_debit, target_debit,
        )
        return best

    # Synthetic fallback for paper/testing if live chain quotes unavailable
    lg.warning(
        'No market candidates matching debit band %.2f - %.2f for %s; generating synthetic candidate',
        min_debit, max_debit, side,
    )
    fallback_otm = 35
    if side == 'C':
        f_long_k = spx_rounded + fallback_otm
        f_short_k = f_long_k + width
    else:
        f_long_k = spx_rounded - fallback_otm
        f_short_k = f_long_k - width

    f_long_sym = build_tastytrade_symbol(expiry_yymmdd, side, f_long_k)
    f_short_sym = build_tastytrade_symbol(expiry_yymmdd, side, f_short_k)
    return DebitSpreadCandidate(
        side=side,
        long_symbol=f_long_sym,
        short_symbol=f_short_sym,
        long_strike=f_long_k,
        short_strike=f_short_k,
        width=width,
        market_debit=target_debit,
        long_mid=round(target_debit + 0.60, 2),
        short_mid=0.60,
        distance_from_target=0.0,
    )
