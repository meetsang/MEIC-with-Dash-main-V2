"""Windows SSL environment sanitization — import before any HTTPS/ssl usage."""
from __future__ import annotations

import os
import sys


def sanitize_ssl_env() -> None:
    """Avast and similar AV tools inject SSLKEYLOGFILE with a pipe path.

    That breaks Python's OpenSSL on Windows (OPENSSL_Uplink / no OPENSSL_Applink).
    """
    if sys.platform != 'win32':
        return
    path = os.environ.get('SSLKEYLOGFILE', '')
    low = path.lower()
    if path.startswith('\\\\.\\') or 'aswmonfltproxy' in low:
        os.environ.pop('SSLKEYLOGFILE', None)


sanitize_ssl_env()
