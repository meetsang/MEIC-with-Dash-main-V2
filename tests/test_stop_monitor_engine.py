"""STOP_MONITOR_ENGINE feature flag (V2.9 rollback shell)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from blocks.stop import run as stop_run


class TestStopMonitorEngine(unittest.TestCase):
    def test_default_is_v2(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.pop('STOP_MONITOR_ENGINE', None)
            self.assertEqual(stop_run.stop_monitor_engine(), 'v2')

    def test_v2_explicit(self):
        with mock.patch.dict(os.environ, {'STOP_MONITOR_ENGINE': 'v2'}):
            self.assertEqual(stop_run.stop_monitor_engine(), 'v2')

    def test_v3_explicit(self):
        with mock.patch.dict(os.environ, {'STOP_MONITOR_ENGINE': 'v3'}):
            self.assertEqual(stop_run.stop_monitor_engine(), 'v3')

    def test_unknown_engine_exits(self):
        with mock.patch.dict(os.environ, {'STOP_MONITOR_ENGINE': 'v99'}):
            with self.assertRaises(SystemExit):
                stop_run.stop_monitor_engine()


if __name__ == '__main__':
    unittest.main()
