#!/usr/bin/env python3
"""List (and optionally kill) MEIC stop-monitor processes.

Works on Windows without PowerShell execution-policy changes.

Usage (from MEIC-with-Dash-main-V2):
  uv run python scripts/check_stop_monitor.py
  uv run python scripts/check_stop_monitor.py --kill
  uv run python scripts/check_stop_monitor.py --kill --force
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
HEARTBEAT_PATH = os.path.join(ROOT, 'trades', 'heartbeat.json')

PATTERNS = (
    'blocks\\stop\\run.py',
    'blocks/stop/run.py',
    'blocks.stop.run',
)


def _find_stop_monitor_processes() -> list[dict]:
    if sys.platform != 'win32':
        return _find_posix()

    ps = (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name = 'python.exe' OR Name = 'pythonw.exe'\" | "
        "Select-Object ProcessId, CreationDate, CommandLine | "
        "ConvertTo-Json -Compress"
    )
    try:
        out = subprocess.check_output(
            ['powershell', '-NoProfile', '-Command', ps],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []

    if not out:
        return []

    data = json.loads(out)
    if isinstance(data, dict):
        data = [data]

    hits = []
    for row in data:
        cmd = str(row.get('CommandLine') or '')
        if any(p in cmd for p in PATTERNS):
            hits.append({
                'pid': int(row['ProcessId']),
                'created': row.get('CreationDate'),
                'cmd': cmd,
            })
    return hits


def _find_posix() -> list[dict]:
    try:
        out = subprocess.check_output(['ps', 'aux'], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    hits = []
    for line in out.splitlines():
        if 'check_stop_monitor' in line:
            continue
        if not any(p in line for p in PATTERNS):
            continue
        parts = line.split(None, 10)
        if len(parts) < 2:
            continue
        hits.append({'pid': int(parts[1]), 'created': None, 'cmd': parts[-1]})
    return hits


def _show_heartbeat() -> None:
    if not os.path.isfile(HEARTBEAT_PATH):
        print('heartbeat.json: (missing)')
        return
    try:
        with open(HEARTBEAT_PATH, encoding='utf-8') as f:
            hb = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f'heartbeat.json: (unreadable) {exc}')
        return
    print('heartbeat.json:')
    print(f"  ts           = {hb.get('ts')}")
    print(f"  engine       = {hb.get('engine')}")
    print(f"  loop_count   = {hb.get('loop_count')}")
    print(f"  active_slots = {hb.get('active_slots')}")
    if 'active_exit_jobs' in hb:
        print(f"  exit_jobs    = {hb.get('active_exit_jobs')}")


def _kill_processes(procs: list[dict], *, force: bool) -> int:
    if not procs:
        print('')
        print('Nothing to kill.')
        return 0
    if not force:
        answer = input(f"Kill {len(procs)} process(es)? [y/N] ").strip().lower()
        if answer != 'y':
            print('Aborted.')
            return 1
    for p in procs:
        pid = p['pid']
        print(f'Stopping PID {pid} ...')
        if sys.platform == 'win32':
            subprocess.run(
                ['taskkill', '/PID', str(pid), '/F'],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            os.kill(pid, 15)
    time.sleep(2)
    remaining = _find_stop_monitor_processes()
    if not remaining:
        print('All stop-monitor processes stopped.')
        return 0
    print(f'{len(remaining)} process(es) still running - check Task Manager')
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description='Check MEIC stop-monitor processes')
    parser.add_argument('--kill', action='store_true', help='Kill matching processes')
    parser.add_argument('--force', action='store_true', help='Skip confirmation prompt')
    args = parser.parse_args()

    print(f'MEIC stop-monitor process check - {ROOT}')
    print('')

    procs = _find_stop_monitor_processes()
    if not procs:
        print('Stop-monitor processes: none found')
    elif len(procs) == 1:
        print('Stop-monitor processes: 1 (expected when launcher is running)')
        for p in procs:
            print(f"  PID {p['pid']}  {p.get('created') or ''}")
            print(f"    {p['cmd']}")
    else:
        print(f'Stop-monitor processes: {len(procs)} - DUPLICATE / ORPHAN RISK')
        for p in procs:
            print(f"  PID {p['pid']}  {p.get('created') or ''}")
            print(f"    {p['cmd']}")

    print('')
    _show_heartbeat()

    if args.kill:
        return _kill_processes(procs, force=args.force)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
