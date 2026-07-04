"""Deprecated — import from ``blocks.entry.spread_scan`` instead."""
from __future__ import annotations

import warnings

from blocks.entry.spread_scan import (  # noqa: F401
    OTM_MAX_DEEP,
    OTM_MAX_DEFAULT,
    OTM_MAX_MODERATE,
    SpreadCandidate,
    pick_meic_candidate,
    resolve_scan_otm_max,
    scan_credit_spreads,
    scan_symbol_list,
)
from blocks.entry.spread_scan import _evaluate_spread, _round_credit  # noqa: F401

warnings.warn(
    'meic0dte.open.spread_scan is deprecated; use blocks.entry.spread_scan',
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    'OTM_MAX_DEEP',
    'OTM_MAX_DEFAULT',
    'OTM_MAX_MODERATE',
    'SpreadCandidate',
    '_evaluate_spread',
    '_round_credit',
    'pick_meic_candidate',
    'resolve_scan_otm_max',
    'scan_credit_spreads',
    'scan_symbol_list',
]
