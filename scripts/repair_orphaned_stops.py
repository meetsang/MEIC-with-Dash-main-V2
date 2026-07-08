#!/usr/bin/env python3
"""Repair orphaned broker stops into trade JSON (explicit operator command only)."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Adopt unclaimed broker stop into trade JSON (dry-run by default)',
    )
    parser.add_argument('--lot', default=None, help='Only repair JSONs matching this lot')
    parser.add_argument(
        '--apply',
        action='store_true',
        help='Write adoption to JSON (default is dry-run only)',
    )
    parser.add_argument('--paper', action='store_true', help='Use paper broker')
    parser.add_argument(
        '--cancel-broker-stops',
        action='store_true',
        help='Repair clean-slate: cancel all BTC on short leg and clear JSON stop',
    )
    args = parser.parse_args(argv)

    from tests.adhoc_integration import cmd_sync_broker_stop

    return cmd_sync_broker_stop(args)


if __name__ == '__main__':
    raise SystemExit(main())
