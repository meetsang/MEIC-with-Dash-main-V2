#!/usr/bin/env python3
"""Run all offline unit tests (no broker credentials required)."""
import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TESTS = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)


def main():
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    start_dir = os.path.dirname(__file__)
    suite.addTests(loader.discover(start_dir, pattern='test_*.py'))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
