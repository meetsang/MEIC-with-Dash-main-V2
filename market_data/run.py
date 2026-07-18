#!/usr/bin/env python3
"""Entry point: python -m market_data.run"""
import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import common.win_ssl_env  # noqa: F401

from market_data.recorder import main

if __name__ == '__main__':
    from common.process_lock import process_lock

    with process_lock('market_data', command='market_data.run'):
        main()
