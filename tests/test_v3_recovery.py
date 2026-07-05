"""V3 recovery stall detection."""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from blocks.stop import state as state_mod
from blocks.stop.v3.recovery import check_exit_stall
from blocks.stop.v3.trade_slot import TradeSlot


class TestV3RecoveryStall(unittest.TestCase):
    def test_exit_stalled_after_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = {
                'status': 'closing',
                'exit_handler': 'manual_close',
                'recovery': {},
                'exit_last_progress_at': (
                    datetime.now(timezone.utc) - timedelta(seconds=200)
                ).isoformat(),
            }
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            with patch('blocks.stop.v3.recovery.v3_config.STOP_EXIT_STALL_SEC', 120):
                stalled = check_exit_stall(slot)

            self.assertTrue(stalled)
            self.assertTrue(slot.state.get('exit_stalled'))

    def test_no_stall_when_recent_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 't.json')
            st = {
                'status': 'closing',
                'exit_handler': 'manual_close',
                'recovery': {},
                'exit_last_progress_at': state_mod.now_iso(),
            }
            state_mod.save_state(path, st)
            slot = TradeSlot.from_path(path)

            with patch('blocks.stop.v3.recovery.v3_config.STOP_EXIT_STALL_SEC', 120):
                stalled = check_exit_stall(slot)

            self.assertFalse(stalled)
