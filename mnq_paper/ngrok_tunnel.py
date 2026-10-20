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


def _find_ngrok_exe() -> Optional[str]:
    exe = shutil.which('ngrok')
    if exe:
        return exe
    import os
    from pathlib import Path
    local_appdata = os.environ.get('LOCALAPPDATA', '')
    if local_appdata:
        winget_pkg = Path(local_appdata) / 'Microsoft' / 'WinGet' / 'Packages'
        if winget_pkg.exists():
            for p in winget_pkg.glob('**/ngrok.exe'):
                if p.is_file():
                    return str(p)
        direct_appdata = Path(local_appdata) / 'ngrok' / 'ngrok.exe'
        if direct_appdata.is_file():
            return str(direct_appdata)
    prog_files = os.environ.get('ProgramFiles', '')
    if prog_files:
        p = Path(prog_files) / 'ngrok' / 'ngrok.exe'
        if p.is_file():
            return str(p)
    return None


_NGROK_PROC: Optional[subprocess.Popen] = None


def stop_ngrok() -> None:
    """Terminate the ngrok subprocess if running."""
    global _NGROK_PROC
    if _NGROK_PROC is not None:
        try:
            log.info("Stopping ngrok subprocess...")
            _NGROK_PROC.terminate()
            _NGROK_PROC.wait(timeout=3)
        except Exception:
            try:
                _NGROK_PROC.kill()
            except Exception:
                pass
        _NGROK_PROC = None


def start_ngrok(port: int, authtoken: str = '', domain: str = '') -> Optional[str]:
    """Start ngrok if the CLI is installed. Returns the public https URL."""
    global _NGROK_PROC
    existing = _tunnels_from_local_api()
    if existing:
        log.info('Reusing existing ngrok tunnel %s', existing)
        return existing

    exe = _find_ngrok_exe()
    if not exe:
        log.warning(
            'ngrok CLI not found. Install from https://ngrok.com/download '
            'or `winget install Ngrok.Ngrok`, then run: ngrok http %s',
            port,
        )
        return None

    args = [exe, 'http', str(port), '--log=stdout']
    if authtoken:
        args.extend(['--authtoken', authtoken])
    if domain:
        args.extend(['--domain', domain])
    log.info('Starting ngrok: %s', ' '.join(args[:3]))
    _NGROK_PROC = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
    )
    import atexit
    atexit.register(stop_ngrok)

    for _ in range(20):
        time.sleep(0.4)
        url = _tunnels_from_local_api()
        if url:
            log.info('ngrok public URL: %s', url)
            return url
    log.warning('ngrok started but public URL was not ready on :4040')
    return None
