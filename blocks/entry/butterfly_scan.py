"""1-2-1 butterfly scanner for Manual Spread tab."""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Dict, List, Optional

import meic0dte.app.config as meic_config
from blocks.entry.spread_scan import _fetch_option_mids_robust
from common.symbols import build_tastytrade_symbol, to_tastytrade
from manual_spread import config as ms_config
from manual_spread.butterfly import (
    ButterflyCandidate,
    STRUCTURE_TYPES,
    net_butterfly_mid,
    strikes_for_body,
)

log = logging.getLogger(__name__)


@dataclass
class ButterflyScanDiagnostics:
    bodies_scanned: int = 0
    skipped_no_quote: int = 0
    skipped_below_min: int = 0
    skipped_incomplete_triplet: int = 0
    spx: float = 0.0
    anchor_strike: int = 0

    def to_dict(self) -> Dict[str, object]:
        return {
            'bodies_scanned': self.bodies_scanned,
            'skipped_no_quote': self.skipped_no_quote,
            'skipped_below_min': self.skipped_below_min,
            'skipped_incomplete_triplet': self.skipped_incomplete_triplet,
            'spx': round(self.spx, 2),
            'anchor_strike': self.anchor_strike,
        }


def _round_strike_up(spx: float) -> int:
    return int(math.ceil(spx / 5) * 5)


def _round_strike_down(spx: float) -> int:
    return int(math.floor(spx / 5) * 5)


def _iter_body_strikes_from_chain(
    snap,
    expiry: str,
    opt_type: str,
    *,
    wing_half: int,
    otm_min: int,
    otm_max: int,
) -> tuple[List[int], int]:
    """Body strikes above 50Δ anchor on grid; returns (bodies, skipped_incomplete)."""
    from common.chain_snapshot import anchor_strike_for_snapshot, strikes_in_snapshot

    half = int(wing_half)
    step = meic_config.STEP
    otm_lo = max(int(otm_min), 5)
    otm_hi = max(int(otm_max), otm_lo)
    ref = anchor_strike_for_snapshot(snap, opt_type)
    strikes = strikes_in_snapshot(snap, expiry, opt_type)
    bodies: List[int] = []
    skipped_incomplete = 0

    if opt_type == 'C':
        min_body = _round_strike_up(ref + half + 0.01)
        max_body = _round_strike_up(ref + otm_hi)
        body = min_body
        while body <= max_body:
            wl, _, wh = strikes_for_body(body, half)
            if wl not in strikes or body not in strikes or wh not in strikes:
                skipped_incomplete += 1
                body += step
                continue
            dist = body - ref
            if dist < otm_lo:
                body += step
                continue
            bodies.append(body)
            body += step
    else:
        max_body = _round_strike_down(ref - half - 0.01)
        min_body = _round_strike_down(ref - otm_hi)
        body = max_body
        while body >= min_body:
            wl, _, wh = strikes_for_body(body, half)
            if wl not in strikes or body not in strikes or wh not in strikes:
                skipped_incomplete += 1
                body -= step
                continue
            dist = ref - body
            if dist < otm_lo:
                body -= step
                continue
            bodies.append(body)
            body -= step
    return bodies, skipped_incomplete


def _iter_body_strikes(
    spx: float,
    opt_type: str,
    *,
    wing_half: int,
    otm_min: int,
    otm_max: int,
) -> List[int]:
    """Body strikes OTM from spot on 5-pt grid (legacy path without loaded chain)."""
    half = int(wing_half)
    otm_lo = max(int(otm_min), 5)
    otm_hi = max(int(otm_max), otm_lo)
    bodies: List[int] = []

    if opt_type == 'C':
        min_body = max(
            _round_strike_up(spx + otm_lo),
            _round_strike_up(spx + half + 0.01),
        )
        max_body = _round_strike_up(spx + otm_hi)
        strike = min_body
        while strike <= max_body:
            wl, _, _ = strikes_for_body(strike, half)
            if wl > spx:
                bodies.append(strike)
            strike += 5
    else:
        max_body = min(
            _round_strike_down(spx - otm_lo),
            _round_strike_down(spx - half - 0.01),
        )
        min_body = _round_strike_down(spx - otm_hi)
        strike = max_body
        while strike >= min_body:
            _, _, wh = strikes_for_body(strike, half)
            if wh < spx:
                bodies.append(strike)
            strike -= 5
    return bodies


def _mid_from_map(
    price_map: Dict[str, float],
    expiry: str,
    opt_type: str,
    strike: int,
    *,
    snap=None,
) -> Optional[float]:
    sym = to_tastytrade(build_tastytrade_symbol(expiry, opt_type, strike))
    val = price_map.get(sym)
    if val is not None and float(val) > 0:
        return float(val)
    bare = sym.lstrip('.')
    val = price_map.get(bare) or price_map.get(f'.{bare}')
    if val is not None and float(val) > 0:
        return float(val)
    if snap is not None:
        for key in (sym, bare, f'.{bare}'):
            v = snap.mids.get(key)
            if v is not None and float(v) > 0:
                return float(v)
    return None


def scan_butterflies(
    broker,
    *,
    structure_type: str,
    expiry: str,
    wing_half: int = 5,
    target_price: float,
    max_results: int = 10,
    otm_min: int = ms_config.BF_OTM_MIN,
    otm_max: int = ms_config.BF_OTM_MAX,
    min_market_price: float = ms_config.MIN_MARKET_CREDIT,
    use_loaded_chain_only: bool = False,
) -> tuple[List[ButterflyCandidate], ButterflyScanDiagnostics]:
    """Scan OTM 1-2-1 butterflies ranked by distance from target credit/debit."""
    key = (structure_type or '').lower()
    meta = STRUCTURE_TYPES.get(key)
    if not meta:
        raise ValueError(f'unsupported structure_type: {structure_type}')

    opt_type = str(meta['opt'])
    sell = bool(meta['sell'])
    side = str(meta['side'])
    diag = ButterflyScanDiagnostics()

    if use_loaded_chain_only:
        from common.chain_snapshot import require_loaded_chain, strikes_in_snapshot

        snap = require_loaded_chain(expiry, int(otm_max))
        ref = snap.anchor_strike or snap.spx_price
        diag.spx = float(ref)
        diag.anchor_strike = int(ref)
        bodies, incomplete = _iter_body_strikes_from_chain(
            snap,
            expiry,
            opt_type,
            wing_half=int(wing_half),
            otm_min=int(otm_min),
            otm_max=int(otm_max),
        )
        diag.skipped_incomplete_triplet = incomplete
        log.info(
            'Butterfly chain scan: anchor=%s bodies=%d incomplete_triplets=%d strikes_in_chain=%d',
            ref,
            len(bodies),
            incomplete,
            len(strikes_in_snapshot(snap, expiry, opt_type)),
        )
    else:
        spx = broker.fetch_spx_price_api() or broker.get_spx_price()
        if not spx:
            raise ValueError('SPX price unavailable')
        diag.spx = float(spx)
        bodies = _iter_body_strikes(
            float(spx),
            opt_type,
            wing_half=int(wing_half),
            otm_min=int(otm_min),
            otm_max=int(otm_max),
        )

    if not bodies:
        return [], diag

    symbols: List[str] = []
    strike_triples: List[tuple[int, int, int]] = []
    for body in bodies:
        wl, _, wh = strikes_for_body(body, int(wing_half))
        strike_triples.append((wl, body, wh))
        for strike in (wl, body, wh):
            symbols.append(build_tastytrade_symbol(expiry, opt_type, strike))

    chain_snap = None
    if use_loaded_chain_only:
        from common.chain_snapshot import get_snapshot

        chain_snap = get_snapshot(expiry)
        anchor = (chain_snap.anchor_strike or chain_snap.spx_price) if chain_snap else diag.spx
        price_map = _fetch_option_mids_robust(
            broker,
            list(dict.fromkeys(symbols)),
            log,
            expiry=expiry,
            otm_max_needed=int(otm_max),
            spx_price=int(anchor),
            allow_warm=False,
        )
    else:
        price_map = _fetch_option_mids_robust(
            broker,
            list(dict.fromkeys(symbols)),
            log,
            expiry=expiry,
            otm_max_needed=int(otm_max),
            spx_price=round(float(diag.spx) / 5) * 5,
            allow_warm=True,
        )
    candidates: List[ButterflyCandidate] = []
    diag.bodies_scanned = len(strike_triples)

    for (wl, body, wh) in strike_triples:
        wl_mid = _mid_from_map(price_map, expiry, opt_type, wl, snap=chain_snap)
        b_mid = _mid_from_map(price_map, expiry, opt_type, body, snap=chain_snap)
        wh_mid = _mid_from_map(price_map, expiry, opt_type, wh, snap=chain_snap)
        if wl_mid is None or b_mid is None or wh_mid is None:
            diag.skipped_no_quote += 1
            continue
        market = net_butterfly_mid(float(wl_mid), float(b_mid), float(wh_mid), sell=sell)
        if market < float(min_market_price):
            diag.skipped_below_min += 1
            continue
        candidates.append(
            ButterflyCandidate(
                structure_type=key,
                side=side,
                opt_type=opt_type,
                body_strike=body,
                wing_low=wl,
                wing_high=wh,
                wing_half=int(wing_half),
                market_price=market,
                wing_low_mid=round(float(wl_mid), 2),
                body_mid=round(float(b_mid), 2),
                wing_high_mid=round(float(wh_mid), 2),
                distance_from_target=round(abs(market - float(target_price)), 2),
                sell=sell,
            )
        )

    candidates.sort(key=lambda c: c.distance_from_target)
    return candidates[: max(1, int(max_results))], diag
