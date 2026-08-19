"""One REST chain warm per MEIC tranche — shared by P and C entry workers."""
from __future__ import annotations

import logging
import threading
from typing import Set

from blocks.entry.spread_scan import resolve_scan_otm_max
from common.chain_snapshot import warm_chain_snapshot
from meic0dte.app import utilities as util

_lock = threading.Lock()
_prepared: Set[str] = set()


def _tranche_key(lot: str) -> str:
    return f'{util.central_date().isoformat()}:{lot}'


def prepare_meic_tranche_chain(
    broker,
    expiry: str,
    lot: str,
    log: logging.Logger,
    *,
    credit_min: float = 0.60,
) -> None:
    """REST-warm the SPX chain once per session lot; no-op if already prepared today."""
    key = _tranche_key(lot)
    with _lock:
        if key in _prepared:
            return
        otm_range = resolve_scan_otm_max(credit_min=float(credit_min))
        warm_chain_snapshot(broker, expiry, log, otm_range=otm_range)
        _prepared.add(key)
        log.info(
            'MEIC tranche chain prepared lot=%s expiry=%s otm_range=%d '
            '(shared REST warm — P/C scans read loaded chain, no per-worker REST)',
            lot,
            expiry,
            otm_range,
        )


def reset_prepared_for_tests() -> None:
    """Test helper — clear in-process tranche prep state."""
    with _lock:
        _prepared.clear()
