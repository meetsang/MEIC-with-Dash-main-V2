"""Move today's trade JSON from history back to active/."""
from __future__ import annotations

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
TODAY_PAT = '20260702'
TODAY_DIR = '2026-07-02'

PAIRS = [
    (os.path.join(ROOT, 'trades/history/MEIC_IC'), os.path.join(ROOT, 'trades/active/MEIC_IC')),
    (
        os.path.join(ROOT, 'trades/history/MANUAL_SPREAD'),
        os.path.join(ROOT, 'trades/active/MANUAL_SPREAD'),
    ),
]


def unarchive_pair(hist_root: str, active_dir: str) -> int:
    os.makedirs(active_dir, exist_ok=True)
    moved: set[str] = set()

    dated = os.path.join(hist_root, TODAY_DIR)
    if os.path.isdir(dated):
        for name in sorted(os.listdir(dated)):
            if not (name.endswith('.json') and TODAY_PAT in name):
                continue
            src = os.path.join(dated, name)
            dst = os.path.join(active_dir, name)
            if not os.path.isfile(dst):
                os.replace(src, dst)
                print(f'moved: {src} -> {dst}')
            else:
                print(f'skip (active exists): {name}')
            moved.add(name)

    for name in sorted(os.listdir(hist_root)):
        if not (name.endswith('.json') and TODAY_PAT in name):
            continue
        src = os.path.join(hist_root, name)
        dst = os.path.join(active_dir, name)
        if name in moved:
            if os.path.isfile(src):
                os.remove(src)
                print(f'removed duplicate: {src}')
            continue
        if os.path.isfile(dst):
            os.remove(src)
            print(f'removed duplicate (active has copy): {src}')
            continue
        os.replace(src, dst)
        print(f'moved: {src} -> {dst}')
        moved.add(name)

    count = len([n for n in os.listdir(active_dir) if n.endswith('.json')])
    print(f'{active_dir}: {count} active file(s)')
    return count


def main() -> int:
    total = 0
    for hist_root, active_dir in PAIRS:
        total += unarchive_pair(hist_root, active_dir)
    print(f'Total restored: {total}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
