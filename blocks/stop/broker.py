"""Re-export broker interface for stop_monitor."""
from brokers.base import BrokerBase, OrderResult

__all__ = ['BrokerBase', 'OrderResult']
