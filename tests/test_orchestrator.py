"""G1 — Orchestrator fires each tranche slot once per day."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, time
from unittest.mock import MagicMock

from orchestrator.scheduler import Orchestrator, TrancheSlot
from strategies.meic.strategy import MEICStrategy


class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.tmp = tempfile.mkdtemp()
        self.pause_file = os.path.join(self.tmp, 'pause_tranches.json')
        with open(self.pause_file, 'w', encoding='utf-8') as f:
            json.dump({'paused_slots': []}, f)

        self.orchestrator = Orchestrator(
            [MEICStrategy()],
            pause_file=self.pause_file,
            fire_tranche=lambda slot: self.fired.append(slot.lot),
        )

    def test_fires_slot_once_when_in_window(self):
        slot = TrancheSlot('11-00', time(10, 59), time(11, 5))
        now = datetime(2026, 6, 24, 11, 0, 0)
        orch = Orchestrator(
            [MagicMock(schedule=lambda: [slot])],
            pause_file=self.pause_file,
            fire_tranche=lambda s: self.fired.append(s.lot),
        )
        orch.tick(now)
        orch.tick(now)
        self.assertEqual(self.fired, ['11-00'])

    def test_skips_paused_lot(self):
        with open(self.pause_file, 'w', encoding='utf-8') as f:
            json.dump({'paused_slots': ['11-00_C', '11-00_P']}, f)
        slot = TrancheSlot('11-00', time(10, 59), time(11, 5))
        orch = Orchestrator(
            [MagicMock(schedule=lambda: [slot])],
            pause_file=self.pause_file,
            fire_tranche=lambda s: self.fired.append(s.lot),
        )
        orch.tick(datetime(2026, 6, 24, 11, 0, 0))
        self.assertEqual(self.fired, [])

    def test_meic_has_six_slots(self):
        slots = MEICStrategy().schedule()
        self.assertEqual(len(slots), 6)
        self.assertEqual(slots[0].lot, '11-00')

    def test_manual_spread_not_scheduled(self):
        from strategies.manual_spread.strategy import ManualSpreadStrategy

        self.assertEqual(ManualSpreadStrategy().schedule(), [])


if __name__ == '__main__':
    unittest.main()
