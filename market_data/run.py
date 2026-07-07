#!/usr/bin/env python3
"""Entry point: python -m market_data.run"""
from market_data.recorder import main

if __name__ == '__main__':
    from common.process_lock import process_lock

    with process_lock('market_data', command='market_data.run'):
        main()
