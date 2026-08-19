from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from mnq_paper.models import ClosedTrade, OpenTrade


class Journal:
    def __init__(self, journal_path: Path, open_path: Path) -> None:
        self.journal_path = journal_path
        self.open_path = open_path
        journal_path.parent.mkdir(parents=True, exist_ok=True)

    def save_open(self, trade: Optional[OpenTrade]) -> None:
        if trade is None:
            if self.open_path.exists():
                self.open_path.unlink()
            return
        self.open_path.write_text(
            json.dumps(trade.to_dict(), indent=2),
            encoding='utf-8',
        )

    def load_open(self) -> Optional[OpenTrade]:
        if not self.open_path.exists():
            return None
        try:
            data = json.loads(self.open_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return None
        if not data:
            return None
        return OpenTrade.from_dict(data)

    def append_closed(self, trade: ClosedTrade) -> None:
        with self.journal_path.open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(trade.to_dict()) + '\n')

    def recent_closed(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self.journal_path.exists():
            return []
        rows: list[dict[str, Any]] = []
        with self.journal_path.open(encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]
