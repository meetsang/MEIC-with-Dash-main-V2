"""Entry worker outcome — returned to Entry Monitor for CSV update."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class EntryWorkerResult:
    slot_key: str
    state: str  # session CSV: entered | failed | entering
    trade_path: str = ''
    order_id: str = ''
    filled_quantity: int = 0
    error: str = ''
    api_status: str = ''  # manual dashboard: placed | partial | working | error
    lot: str = ''
    filename: str = field(default='')

    def __post_init__(self) -> None:
        if self.trade_path and not self.filename:
            self.filename = os.path.basename(self.trade_path)

    def to_manual_api_dict(self) -> dict:
        status = self.api_status or (
            'error' if self.state == 'failed' else 'working' if self.filled_quantity == 0 else 'placed'
        )
        out = {
            'status': status,
            'slot_key': self.slot_key,
            'lot': self.lot,
            'trade_path': self.trade_path,
            'filled_quantity': self.filled_quantity,
        }
        if self.order_id:
            out['order_id'] = self.order_id
        if self.filename:
            out['filename'] = self.filename
        if self.error:
            out['error'] = self.error
        return out
