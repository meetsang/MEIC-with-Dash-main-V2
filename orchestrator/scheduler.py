"""Load strategies from YAML and fire scheduled tranche entry windows."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Iterable, List, Optional, Set, Tuple

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class TrancheSlot:
    """One scheduled entry window for a strategy."""

    lot: str
    window_start: time
    window_end: time
    strategy_name: str = 'MEIC_IC'

    def key(self) -> Tuple[str, str, time, time]:
        return (self.strategy_name, self.lot, self.window_start, self.window_end)

    def is_in_window(self, now_time: time) -> bool:
        return self.window_start <= now_time <= self.window_end


class Orchestrator:
    """Fire strategy tranche slots once per day when the clock is in-window."""

    def __init__(
        self,
        strategies: Iterable,
        *,
        pause_file: str,
        fire_tranche: Callable[[TrancheSlot], None],
        logger: Optional[logging.Logger] = None,
    ):
        self.strategies = list(strategies)
        self.pause_file = pause_file
        self.fire_tranche = fire_tranche
        self.log = logger or log
        self._fired: Set[Tuple[str, str, time, time]] = set()

    def scheduled_slots(self) -> List[TrancheSlot]:
        slots: List[TrancheSlot] = []
        for strategy in self.strategies:
            schedule_fn = getattr(strategy, 'schedule', None)
            if not callable(schedule_fn):
                continue
            slots.extend(schedule_fn())
        return slots

    def _is_paused(self, lot: str) -> bool:
        """True when both C and P sides of a lot are paused via dashboard."""
        try:
            with open(self.pause_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            paused = set(data.get('paused_slots', []))
            return f'{lot}_C' in paused and f'{lot}_P' in paused
        except Exception:
            return False

    def tick(self, now: datetime) -> None:
        """Evaluate all strategy slots; fire any that are due and not yet fired."""
        now_time = now.time()
        for slot in self.scheduled_slots():
            key = slot.key()
            if key in self._fired:
                continue
            if not slot.is_in_window(now_time):
                continue
            if self._is_paused(slot.lot):
                self._fired.add(key)
                self.log.info(
                    'Tranche %s (%s) skipped — paused via dashboard',
                    slot.lot,
                    slot.strategy_name,
                )
                continue
            self._fired.add(key)
            self.log.info(
                'Firing tranche %s for strategy %s',
                slot.lot,
                slot.strategy_name,
            )
            self.fire_tranche(slot)

    def any_fired(self) -> bool:
        return bool(self._fired)

    def strategy_names(self) -> List[str]:
        names: List[str] = []
        for strategy in self.strategies:
            cfg = getattr(strategy, 'config', None)
            name = getattr(cfg, 'name', None) or type(strategy).__name__
            names.append(str(name))
        return names
