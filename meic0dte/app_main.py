import os
import sys

current_dir = os.path.abspath(os.path.dirname(__file__))
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)
        break
    current_dir = os.path.dirname(current_dir)

from app import utilities, vertical, vertical_thin
from blocks.entry.fire_lot import fire_meic_lot, project_root_from_caller
from common.broker_factory import use_thin_tranches


def _legacy_tranche(lot: str) -> None:
    """Integration / rollback path — vertical_thin subprocess model."""
    if use_thin_tranches():
        vertical_thin.tranche(lot)
    else:
        vertical.tranche(lot)


def SPX_IC_tranche():
    lot = os.environ.get('MEIC_LOT') or utilities.get_lot_time()
    print(f'lot={lot}')

    if os.environ.get('MEIC_USE_LEGACY_ENTRY', '').strip() == '1':
        print('MEIC_USE_LEGACY_ENTRY=1 — legacy vertical_thin path')
        _legacy_tranche(lot)
        return

    root = project_root_from_caller(current_dir)
    if fire_meic_lot(root, lot):
        return

    print(f'No session CSV rows for lot={lot} — legacy vertical_thin fallback')
    _legacy_tranche(lot)


if __name__ == '__main__':
    SPX_IC_tranche()
