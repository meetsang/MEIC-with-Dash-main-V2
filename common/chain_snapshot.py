"""In-process SPX option mid snapshot cache for scan paths.

Warms a ±band around spot once per expiry; manual spread keeps the snapshot
until Load Chain replaces it or Clear is used (no auto-expiry by default).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import meic0dte.app.config as meic_config
from common.symbols import build_tastytrade_symbol, parse_canonical, to_tastytrade

log = logging.getLogger(__name__)

CHAIN_SNAPSHOT_ENABLED = os.environ.get('CHAIN_SNAPSHOT_ENABLED', 'true').lower() in (
    '1', 'true', 'yes',
)
# 0 = no auto-expiry; chain stays until Load Chain replaces it or Clear is used.
CHAIN_SNAPSHOT_TTL_SEC = float(os.environ.get('CHAIN_SNAPSHOT_TTL_SEC', '0'))
CHAIN_SNAPSHOT_DEFAULT_OTM_RANGE = int(os.environ.get('CHAIN_SNAPSHOT_DEFAULT_OTM_RANGE', '300'))
REST_MARKET_DATA_CHUNK_SIZE = int(os.environ.get('REST_MARKET_DATA_CHUNK_SIZE', '100'))
CHAIN_SNAPSHOT_FETCH_GREEKS = os.environ.get('CHAIN_SNAPSHOT_FETCH_GREEKS', 'true').lower() in (
    '1', 'true', 'yes',
)
CALL_FIFTY_DELTA = 0.50
PUT_FIFTY_DELTA = -0.50


class ChainSnapshotNotLoaded(RuntimeError):
    """Raised when a scan requires a pre-loaded chain snapshot."""

    def __init__(
        self,
        expiry: str,
        otm_max_needed: int,
        *,
        reason: str = 'not_loaded',
    ):
        self.expiry = expiry
        self.otm_max_needed = int(otm_max_needed)
        self.reason = reason
        if reason == 'band_insufficient':
            loaded = get_snapshot(expiry)
            band = loaded.otm_range if loaded else 0
            msg = (
                f'Loaded chain band is ±{band} pts but search OTM needed is '
                f'{otm_max_needed} — Load Chain again with wider Chain ± pts'
            )
        else:
            msg = (
                f'Chain not loaded for expiry {expiry} — click Load Chain first'
            )
        super().__init__(msg)


@dataclass
class ChainSnapshot:
    expiry: str
    captured_at: float
    spx_price: int
    otm_range: int
    mids: Dict[str, float] = field(default_factory=dict)
    deltas: Dict[str, float] = field(default_factory=dict)
    anchor_strike: int = 0  # ~50-delta reference from chain (not REST SPX)

    def age_sec(self) -> float:
        return max(0.0, time.time() - self.captured_at)

    def covers_otm(self, otm_max: int) -> bool:
        return self.otm_range >= int(otm_max)

    def is_fresh(self, ttl_sec: float = CHAIN_SNAPSHOT_TTL_SEC) -> bool:
        if ttl_sec <= 0:
            return True
        return self.age_sec() <= ttl_sec


_snapshots: Dict[str, ChainSnapshot] = {}


def default_rest_chunk_size() -> int:
    return REST_MARKET_DATA_CHUNK_SIZE


def round_spx_strike(spx: float) -> int:
    return int(round(spx / meic_config.STEP) * meic_config.STEP)


def _parse_snapshot_symbol(sym: str, expiry: str) -> Optional[tuple[str, int]]:
    parsed = parse_canonical(sym.lstrip('.'))
    if not parsed:
        parsed = parse_canonical(sym)
    if not parsed or parsed[0] != expiry:
        return None
    return parsed[1], int(parsed[2])


def strikes_in_snapshot(snap: ChainSnapshot, expiry: str, opt_type: str) -> set[int]:
    strikes: set[int] = set()
    want = opt_type.upper()
    for sym in snap.mids:
        parsed = _parse_snapshot_symbol(sym, expiry)
        if parsed and parsed[0] == want:
            strikes.add(parsed[1])
    return strikes


def call_fifty_delta_strike(expiry: str, deltas: Dict[str, float]) -> Optional[int]:
    """Highest call strike with delta >= 0.50 (~ATM reference)."""
    best: Optional[int] = None
    for sym, delta in deltas.items():
        parsed = _parse_snapshot_symbol(sym, expiry)
        if not parsed or parsed[0] != 'C':
            continue
        if float(delta) >= CALL_FIFTY_DELTA:
            strike = parsed[1]
            if best is None or strike > best:
                best = strike
    return best


def put_fifty_delta_strike(expiry: str, deltas: Dict[str, float]) -> Optional[int]:
    """Lowest put strike with delta <= -0.50 (~ATM reference)."""
    best: Optional[int] = None
    for sym, delta in deltas.items():
        parsed = _parse_snapshot_symbol(sym, expiry)
        if not parsed or parsed[0] != 'P':
            continue
        if float(delta) <= PUT_FIFTY_DELTA:
            strike = parsed[1]
            if best is None or strike < best:
                best = strike
    return best


def anchor_strike_for_snapshot(snap: ChainSnapshot, opt_type: str = 'C') -> int:
    """Best scan anchor: 50-delta from chain, else REST SPX at load."""
    expiry = snap.expiry
    if opt_type.upper() == 'C' and snap.deltas:
        ref = call_fifty_delta_strike(expiry, snap.deltas)
        if ref is not None:
            return ref
    if opt_type.upper() == 'P' and snap.deltas:
        ref = put_fifty_delta_strike(expiry, snap.deltas)
        if ref is not None:
            return ref
    return int(snap.spx_price)


def build_band_symbols(
    expiry: str,
    spx_price: int,
    otm_range: int,
    *,
    step: int = meic_config.STEP,
) -> List[str]:
    """All call + put streamer symbols from spot − otm_range to spot + otm_range."""
    low = int(spx_price) - int(otm_range)
    high = int(spx_price) + int(otm_range)
    symbols: List[str] = []
    strike = low
    while strike <= high:
        symbols.append(build_tastytrade_symbol(expiry, 'C', strike))
        symbols.append(build_tastytrade_symbol(expiry, 'P', strike))
        strike += step
    return list(dict.fromkeys(symbols))


def get_snapshot(expiry: str) -> Optional[ChainSnapshot]:
    return _snapshots.get(expiry)


def clear_snapshot(expiry: str | None = None) -> None:
    if expiry is None:
        _snapshots.clear()
    else:
        _snapshots.pop(expiry, None)


def snapshot_status(expiry: str) -> Dict[str, object]:
    snap = get_snapshot(expiry)
    if snap is None:
        return {
            'loaded': False,
            'fresh': False,
            'expiry': expiry,
        }
    return {
        'loaded': True,
        'fresh': snap.is_fresh(),
        'expiry': expiry,
        'spx': snap.anchor_strike or snap.spx_price,
        'anchor_strike': snap.anchor_strike or snap.spx_price,
        'rest_spx_at_load': snap.spx_price,
        'otm_range': snap.otm_range,
        'symbol_count': len(snap.mids),
        'delta_count': len(snap.deltas),
        'age_sec': round(snap.age_sec(), 1),
        'expires': CHAIN_SNAPSHOT_TTL_SEC > 0,
        'ttl_sec': int(CHAIN_SNAPSHOT_TTL_SEC) if CHAIN_SNAPSHOT_TTL_SEC > 0 else None,
    }


def require_loaded_chain(expiry: str, otm_max_needed: int) -> ChainSnapshot:
    """Return loaded snapshot or raise ChainSnapshotNotLoaded."""
    needed = int(otm_max_needed)
    snap = get_snapshot(expiry)
    if snap is None:
        raise ChainSnapshotNotLoaded(expiry, needed, reason='not_loaded')
    if not snap.covers_otm(needed):
        raise ChainSnapshotNotLoaded(expiry, needed, reason='band_insufficient')
    return snap


def lookup_mids_from_snapshot(
    symbols: List[str],
    expiry: str,
    otm_max_needed: int,
) -> Dict[str, float]:
    """Read mids from a pre-loaded snapshot only (no REST)."""
    snap = require_loaded_chain(expiry, otm_max_needed)
    out: Dict[str, float] = {}
    for sym in symbols:
        tt = to_tastytrade(sym)
        mid = snap.mids.get(tt)
        if mid is not None:
            out[tt] = mid
    return out


def _fetch_mids_chunked(
    broker,
    symbols: List[str],
    logger: logging.Logger,
    *,
    chunk_size: int | None = None,
) -> Dict[str, float]:
    batch_size = chunk_size if chunk_size is not None else default_rest_chunk_size()
    unique = list(dict.fromkeys(symbols))
    out: Dict[str, float] = {}
    for i in range(0, len(unique), batch_size):
        batch = unique[i:i + batch_size]
        out.update(broker.fetch_option_mids_api(batch))

    missing = [sym for sym in unique if to_tastytrade(sym) not in out]
    if missing:
        for i in range(0, len(missing), 10):
            retry_batch = missing[i:i + 10]
            out.update(broker.fetch_option_mids_api(retry_batch))
    return out


def warm_chain_snapshot(
    broker,
    expiry: str,
    logger: logging.Logger,
    *,
    otm_range: int | None = None,
    spx_price: int | None = None,
) -> ChainSnapshot:
    """REST-fetch mids for the full ±otm_range SPX band and store in-process."""
    band = int(otm_range if otm_range is not None else CHAIN_SNAPSHOT_DEFAULT_OTM_RANGE)
    if spx_price is None:
        spx = broker.fetch_spx_price_api() or broker.get_spx_price()
        if spx is None:
            raise RuntimeError('SPX price unavailable for chain snapshot warm')
        spx_price = round_spx_strike(float(spx))
    else:
        spx_price = int(spx_price)

    symbols = build_band_symbols(expiry, spx_price, band)
    logger.info(
        'Warming SPX chain snapshot expiry=%s spot=%s otm_range=%d (%d symbols)',
        expiry, spx_price, band, len(symbols),
    )
    mids = _fetch_mids_chunked(broker, symbols, logger)
    got = len(mids)
    expected = len(symbols)
    if got < expected:
        logger.warning(
            'Chain snapshot quotes %d/%d — reload or widen band if near-ATM strikes missing',
            got, expected,
        )
    deltas: Dict[str, float] = {}
    if CHAIN_SNAPSHOT_FETCH_GREEKS:
        try:
            from common.option_greeks import fetch_option_deltas_sync

            deltas = fetch_option_deltas_sync(broker, list(symbols))
        except Exception as exc:
            logger.warning('Chain snapshot greeks fetch failed: %s', exc)

    anchor = call_fifty_delta_strike(expiry, deltas) or int(spx_price)
    snap = ChainSnapshot(
        expiry=expiry,
        captured_at=time.time(),
        spx_price=spx_price,
        otm_range=band,
        mids=dict(mids),
        deltas=dict(deltas),
        anchor_strike=int(anchor),
    )
    _snapshots[expiry] = snap
    logger.info(
        'Chain snapshot ready: %d/%d symbols, %d deltas, anchor=%s (REST spot=%s, otm_range=%d)',
        len(snap.mids), len(symbols), len(deltas), anchor, spx_price, band,
    )
    return snap


def _ensure_snapshot(
    broker,
    expiry: str,
    otm_max_needed: int,
    logger: logging.Logger,
    *,
    spx_price: int | None = None,
    allow_warm: bool = True,
) -> Optional[ChainSnapshot]:
    if not CHAIN_SNAPSHOT_ENABLED:
        return None

    needed = int(otm_max_needed)
    snap = get_snapshot(expiry)
    if snap and snap.is_fresh() and snap.covers_otm(needed):
        return snap

    if not allow_warm:
        return snap

    warm_range = max(needed, CHAIN_SNAPSHOT_DEFAULT_OTM_RANGE)
    try:
        return warm_chain_snapshot(
            broker,
            expiry,
            logger,
            otm_range=warm_range,
            spx_price=spx_price,
        )
    except Exception as exc:
        logger.warning('Chain snapshot warm failed: %s', exc)
        return snap


def fetch_entry_mids_fresh(
    broker,
    symbols: List[str],
    expiry: str,
    logger: logging.Logger,
    *,
    chunk_size: int | None = None,
) -> Dict[str, float]:
    """REST-fetch every requested symbol — never serve cached snapshot mids (MEIC entry)."""
    unique = list(dict.fromkeys(symbols))
    out = _fetch_mids_chunked(broker, unique, logger, chunk_size=chunk_size)
    snap = get_snapshot(expiry)
    if snap:
        snap.mids.update(out)
    got = len({to_tastytrade(s) for s in unique if to_tastytrade(s) in out})
    logger.info(
        'Entry REST fresh fetch expiry=%s (%d/%d symbols)',
        expiry, got, len(unique),
    )
    return out


def fetch_option_mids_with_snapshot(
    broker,
    symbols: List[str],
    expiry: str,
    logger: logging.Logger,
    *,
    otm_max_needed: int,
    spx_price: int | None = None,
    chunk_size: int | None = None,
    allow_warm: bool = True,
) -> Dict[str, float]:
    """
    Return mids for symbols, preferring a warm chain snapshot when enabled.

    When allow_warm=False, only reads a pre-loaded snapshot (no REST warm/fetch).
    """
    unique = list(dict.fromkeys(symbols))
    out: Dict[str, float] = {}

    if not allow_warm:
        out = lookup_mids_from_snapshot(unique, expiry, otm_max_needed)
        got = len({to_tastytrade(s) for s in unique if to_tastytrade(s) in out})
        logger.info(
            'Loaded chain lookup expiry=%s (%d/%d symbols)',
            expiry, got, len(unique),
        )
        return out

    snap = _ensure_snapshot(
        broker, expiry, otm_max_needed, logger, spx_price=spx_price, allow_warm=True,
    )
    if snap:
        for sym in unique:
            tt = to_tastytrade(sym)
            mid = snap.mids.get(tt)
            if mid is not None:
                out[tt] = mid
        if len(out) == len({to_tastytrade(s) for s in unique}):
            logger.info(
                'Chain snapshot hit expiry=%s (%d/%d symbols, age=%.0fs)',
                expiry, len(out), len(unique), snap.age_sec(),
            )
            return out
        missing = [sym for sym in unique if to_tastytrade(sym) not in out]
        logger.info(
            'Chain snapshot partial expiry=%s (%d/%d) — fetching %d missing',
            expiry, len(out), len(unique), len(missing),
        )
        extra = _fetch_mids_chunked(broker, missing, logger, chunk_size=chunk_size)
        out.update(extra)
        snap.mids.update(extra)
        return out

    out = _fetch_mids_chunked(broker, unique, logger, chunk_size=chunk_size)
    got = len({to_tastytrade(s) for s in unique if to_tastytrade(s) in out})
    logger.info('API quotes received for %d/%d symbols (no snapshot)', got, len(unique))
    if got < len(unique) * 0.5:
        logger.warning(
            'Low quote coverage (%d/%d) — scan may skew toward strikes with REST data',
            got, len(unique),
        )
    return out
