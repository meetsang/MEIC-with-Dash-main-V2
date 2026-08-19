"""Optional ngrok tunnel so a friend can POST signals from outside the LAN."""
from __future__ import annotations

import logging
import shutil
import subprocess
import time
from typing import Optional
from urllib.request import urlopen

log = logging.getLogger('mnq_paper.ngrok')


def _tunnels_from_local_api() -> Optional[str]:
    try:
        import json
        with urlopen('http://127.0.0.1:4040/api/tunnels', timeout=2) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        for tunnel in data.get('tunnels') or []:
            url = tunnel.get('public_url') or ''
            if url.startswith('https://'):
                return url
        for tunnel in data.get('tunnels') or []:
            url = tunnel.get('public_url') or ''
            if url:
                return url
    except Exception:
        return None
    return None


def start_ngrok(port: int, authtoken: str = '') -> Optional[str]:
    """Start ngrok if the CLI is installed. Returns the public https URL."""
    existing = _tunnels_from_local_api()
    if existing:
        log.info('Reusing existing ngrok tunnel %s', existing)
        return existing

    exe = shutil.which('ngrok')
    if not exe:
        log.warning(
            'ngrok CLI not found. Install from https://ngrok.com/download '
            'or `winget install ngrok.ngrok`, then run: ngrok http %s',
            port,
        )
        return None

    args = [exe, 'http', str(port), '--log=stdout']
    if authtoken:
        args.extend(['--authtoken', authtoken])
    log.info('Starting ngrok: %s', ' '.join(args[:3]))
    subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    for _ in range(20):
        time.sleep(0.4)
        url = _tunnels_from_local_api()
        if url:
            log.info('ngrok public URL: %s', url)
            return url
    log.warning('ngrok started but public URL was not ready on :4040')
    return None
