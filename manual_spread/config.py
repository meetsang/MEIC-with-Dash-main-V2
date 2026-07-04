"""Defaults for Manual Spread Strategy dashboard entry."""

STRATEGY = 'MANUAL_SPREAD'
DEFAULT_UNDERLYING = 'SPX'
DEFAULT_SPREAD_WIDTH = 25
DEFAULT_TARGET_CREDIT = 0.60
DEFAULT_QUANTITY = 1
SCAN_MAX_RESULTS = 10
OTM_MIN = 5
OTM_MAX = 250
# Low target credits need deeper OTM; MEIC band (~0.9–1.85) only needs ~150.
OTM_MAX_LOW_TARGET = 300
MIN_MARKET_CREDIT = 0.05
# Entry worker polls brokerage at least this long before handing off to pending_fill_sync.
OPEN_FILL_POLL_SEC = 90
