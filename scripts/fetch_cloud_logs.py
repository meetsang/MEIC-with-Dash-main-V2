"""
Fetch & Summarize MEIC Logs (Local or Google Cloud Logging)
===========================================================
Token-efficient CLI helper for local Antigravity AI agent debugging workflows.

Usage:
  # Scan local logs/ for today's WARNING/ERROR entries:
  python scripts/fetch_cloud_logs.py --local

  # Query Google Cloud Logging for today's WARNING/ERROR entries:
  python scripts/fetch_cloud_logs.py --date 2026-11-07 --severity WARNING --limit 100

  # Sync trade JSONs, data/ CSVs, and SQLite DB from GCP VM over Tailscale:
  python scripts/fetch_cloud_logs.py --sync-snapshot --vm-host meic-bot-prod
"""

from __future__ import annotations

import argparse
import datetime
import glob
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
_LEVEL_RE = re.compile(r'\[(WARNING|ERROR|CRITICAL)\]', re.IGNORECASE)


def scan_local_logs(target_date: str, service_filter: str = '', limit: int = 100) -> int:
    """Scan local `logs/*.log` and `meic0dte/logs/*.log` for WARNING/ERROR/CRITICAL lines."""
    patterns = [
        os.path.join(ROOT, 'logs', f'*{target_date}*.log'),
        os.path.join(ROOT, 'meic0dte', 'logs', '*.log'),
    ]
    files: list[str] = []
    for pat in patterns:
        files.extend(sorted(glob.glob(pat)))

    if service_filter:
        files = [f for f in files if service_filter.lower() in os.path.basename(f).lower()]

    print(f'--- Scanning {len(files)} local log file(s) for {target_date} ---')
    printed = 0
    for path in files:
        bname = os.path.basename(path)
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as fh:
                lines = fh.readlines()
        except OSError:
            continue

        i = 0
        while i < len(lines):
            line = lines[i].rstrip()
            if _LEVEL_RE.search(line) or 'Traceback (most recent call last):' in line:
                if target_date in bname or target_date in line or 'Traceback' in line:
                    print(f'[{bname}] {line}')
                    printed += 1
                    # Include up to 8 lines of stack trace if present
                    j = i + 1
                    while j < len(lines) and j <= i + 8 and (lines[j].startswith(' ') or 'Error' in lines[j] or 'Exception' in lines[j]):
                        print(f'[{bname}]   {lines[j].rstrip()}')
                        j += 1
                    i = j
                    if printed >= limit:
                        print(f'--- Reached limit ({limit} entries) ---')
                        return printed
                    continue
            i += 1

    if printed == 0:
        print('No WARNING/ERROR/CRITICAL entries found.')
    return printed


def fetch_gcp_logs(target_date: str, severity: str = 'WARNING', service_filter: str = '', limit: int = 100) -> int:
    """Query Google Cloud Logging via `google-cloud-logging` or `gcloud logging read`."""
    filter_parts = [
        'resource.type="gce_instance"',
        f'severity>={severity.upper()}',
        f'timestamp>="{target_date}T00:00:00Z"',
    ]
    if service_filter:
        filter_parts.append(f'textPayload:"{service_filter}"')
    filter_expr = ' AND '.join(filter_parts)

    try:
        from google.cloud import logging as gcp_logging  # type: ignore

        client = gcp_logging.Client()
        print(f'--- Fetching GCP Cloud Logging ({filter_expr}) ---')
        count = 0
        for entry in client.list_entries(filter_=filter_expr, page_size=limit, max_results=limit):
            ts = entry.timestamp.strftime('%Y-%m-%d %H:%M:%S') if entry.timestamp else '?'
            payload = entry.payload if isinstance(entry.payload, str) else str(entry.payload)
            print(f'[{ts}] [{entry.severity}] {payload.strip()}')
            count += 1
        return count
    except ImportError:
        cmd = [
            'gcloud',
            'logging',
            'read',
            filter_expr,
            f'--limit={limit}',
            '--format=value(timestamp,severity,textPayload)',
        ]
        print(f'Running: {" ".join(cmd)}')
        res = subprocess.run(cmd, check=False)
        return res.returncode


def sync_cloud_snapshot(vm_host: str, remote_dir: str = '/opt/meic') -> int:
    """Pull trade JSONs, data/ CSVs, and dashboard/trades.db from the cloud VM via Tailscale SCP/rsync."""
    targets = ['trades', 'data', 'dashboard/trades.db']
    for rel in targets:
        src = f'{vm_host}:{remote_dir}/{rel}'
        dest = os.path.join(ROOT, rel if rel.endswith('.db') else '')
        cmd = ['scp', '-r', src, dest]
        print(f'Syncing {src} -> {dest} ...')
        subprocess.run(cmd, check=False)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='Fetch MEIC logs (local or GCP Cloud Logging)')
    parser.add_argument('--local', action='store_true', help='Scan local logs/ directory instead of GCP')
    parser.add_argument('--date', default=datetime.date.today().isoformat(), help='Date YYYY-MM-DD (default: today)')
    parser.add_argument('--severity', default='WARNING', help='Minimum severity (WARNING, ERROR, CRITICAL)')
    parser.add_argument('--service', default='', help='Optional service substring filter (e.g. launcher, stream, drma)')
    parser.add_argument('--limit', type=int, default=100, help='Max log entries to display')
    parser.add_argument('--sync-snapshot', action='store_true', help='Sync trades/, data/, and trades.db from cloud VM')
    parser.add_argument('--vm-host', default=os.environ.get('MEIC_VM_HOST', 'meic-bot-prod'), help='Tailscale VM hostname')
    args = parser.parse_args()

    if args.sync_snapshot:
        sync_cloud_snapshot(args.vm_host)
    if args.local:
        scan_local_logs(args.date, service_filter=args.service, limit=args.limit)
        return 0
    return fetch_gcp_logs(args.date, severity=args.severity, service_filter=args.service, limit=args.limit)


if __name__ == '__main__':
    sys.exit(main())
