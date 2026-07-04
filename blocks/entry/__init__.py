"""Credit spread entry building block."""
from blocks.entry.config import CreditEntryConfig
from blocks.entry.credit_spread import CreditSpreadEntry
from blocks.entry.spread_scan import SpreadCandidate, scan_credit_spreads

__all__ = ['CreditEntryConfig', 'CreditSpreadEntry', 'SpreadCandidate', 'scan_credit_spreads']
