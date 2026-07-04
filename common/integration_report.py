"""Append-only integration session report (meic0dte/trades/integration_report.json)."""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any, Dict, List

from common import trades_layout

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
REPORT_PATH = trades_layout.ops_path('integration_report.json')


def _load() -> List[Dict[str, Any]]:
    if not os.path.exists(REPORT_PATH):
        return []
    try:
        with open(REPORT_PATH, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def append_event(event: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
    events = _load()
    event = dict(event)
    event.setdefault('ts', datetime.now().isoformat(timespec='seconds'))
    events.append(event)
    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(events, f, indent=2)


def clear() -> None:
    if os.path.exists(REPORT_PATH):
        os.remove(REPORT_PATH)
