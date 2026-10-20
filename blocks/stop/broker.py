"""Re-export broker interface for stop_monitor with per-trade broker routing."""
from typing import Any, Optional
from brokers.base import BrokerBase, OrderResult

__all__ = ['BrokerBase', 'OrderResult', 'resolve_broker_for_trade']


def resolve_broker_for_trade(state_or_slot: Any, default_broker: Optional[BrokerBase] = None) -> BrokerBase:
    """Resolve the correct broker (real TastyTradeBroker or JournalBroker) for a specific trade.

    Ensures live trades send orders to the exchange and paper trades only hit JournalBroker.
    """
    from common.broker_factory import _live_broker_blocked_in_tests

    if default_broker is not None and _live_broker_blocked_in_tests():
        return default_broker

    state = getattr(state_or_slot, 'state', None)
    if not isinstance(state, dict):
        state = state_or_slot if isinstance(state_or_slot, dict) else {}

    strategy = (state.get('entry') or {}).get('strategy') or ''
    open_oid = str(
        state.get('open_order_id')
        or (state.get('open_order') or {}).get('order_id')
        or ''
    )

    # 1. If state explicitly stamps paper mode, honor trade provenance
    if state.get('paper') is True or open_oid.startswith('journal-'):
        from common.broker_factory import get_shared_broker_for_strategy
        return get_shared_broker_for_strategy(strategy or 'MEIC_IC', paper=True)
    if state.get('paper') is False:
        from common.broker_factory import get_shared_broker_for_strategy
        return get_shared_broker_for_strategy(strategy or 'MEIC_IC', paper=False)

    # 2. Check if strategy is configured as paper
    if strategy:
        from common.broker_factory import is_strategy_paper, get_shared_broker_for_strategy
        is_paper = is_strategy_paper(strategy)
        return get_shared_broker_for_strategy(strategy, paper=is_paper)

    if default_broker is not None:
        return default_broker

    from common.broker_factory import get_shared_broker
    return get_shared_broker()

