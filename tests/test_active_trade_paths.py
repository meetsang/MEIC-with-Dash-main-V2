"""Tests for stop_monitor active trade path filtering."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from blocks.stop import state as stop_state


class TestActiveTradePaths(unittest.TestCase):
    def test_skips_session_and_skyline_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = os.path.join(tmp, 'trades', 'active', 'SKYLINE_SPX')
            os.makedirs(active, exist_ok=True)
            open(os.path.join(active, 'session_2026-07-31.json'), 'w').close()
            open(os.path.join(active, 'skyline_2026-07-31.json'), 'w').close()
            open(os.path.join(active, 'halted_2026-07-31.json'), 'w').close()
            vert = os.path.join(active, '11-00_P_20260731T105900.json')
            open(vert, 'w').close()

            with mock.patch.object(stop_state, '_project_root', return_value=tmp):
                paths = list(stop_state.iter_active_trade_paths())

            self.assertEqual(paths, [vert])


if __name__ == '__main__':
    unittest.main()
