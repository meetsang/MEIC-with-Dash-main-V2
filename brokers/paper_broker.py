"""Paper trading broker wrapper — uses tastyware PaperSession when PAPER_MODE=true."""
from __future__ import annotations

from brokers.tastytrade_broker import TastyTradeBroker


class PaperBroker(TastyTradeBroker):
    """
    TastyTradeBroker configured with PaperSession.
    Instantiated via broker_factory.get_broker(paper=True).
    """

    pass
