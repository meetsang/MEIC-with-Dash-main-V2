from brokers.base import BrokerBase, ComboLeg, OrderResult

__all__ = ['BrokerBase', 'ComboLeg', 'OrderResult', 'TastyTradeBroker']


def __getattr__(name: str):
    if name == 'TastyTradeBroker':
        from brokers.tastytrade_broker import TastyTradeBroker
        return TastyTradeBroker
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')
