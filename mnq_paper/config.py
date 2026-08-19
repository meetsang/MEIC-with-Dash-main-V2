"""MNQ paper-bot settings. Reuses TastyTrade OAuth from the MEIC .env."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = Path(__file__).resolve().parent
DATA_DIR = PACKAGE_DIR / 'data'

load_dotenv(ROOT / '.env')


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    return float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    return int(raw)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


@dataclass(frozen=True)
class Settings:
    product_code: str
    rest_symbol: str
    stop_points: float
    target_points: float
    qty: int
    tick: float
    point_value: float
    webhook_secret: str
    host: str
    port: int
    ngrok_enabled: bool
    ngrok_authtoken: str
    data_dir: Path

    @property
    def journal_path(self) -> Path:
        return self.data_dir / 'trades.jsonl'

    @property
    def open_path(self) -> Path:
        return self.data_dir / 'open.json'

    @property
    def secret_path(self) -> Path:
        return self.data_dir / 'webhook_secret.txt'


def _default_secret(data_dir: Path) -> str:
    path = data_dir / 'webhook_secret.txt'
    if path.exists():
        saved = path.read_text(encoding='utf-8').strip()
        if saved:
            return saved
    token = secrets.token_urlsafe(24)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding='utf-8')
    return token


def load_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rest_symbol = os.getenv('MNQ_SYMBOL', '/MNQU6').strip() or '/MNQU6'
    if not rest_symbol.startswith('/'):
        rest_symbol = '/' + rest_symbol
    product = os.getenv('MNQ_PRODUCT', 'MNQ').strip().upper() or 'MNQ'
    env_secret = os.getenv('MNQ_WEBHOOK_SECRET', '').strip()
    secret = env_secret or _default_secret(DATA_DIR)
    return Settings(
        product_code=product,
        rest_symbol=rest_symbol,
        stop_points=_float('MNQ_STOP_POINTS', 20.0),
        target_points=_float('MNQ_TARGET_POINTS', 40.0),
        qty=_int('MNQ_QTY', 1),
        tick=_float('MNQ_TICK', 0.25),
        point_value=_float('MNQ_POINT_VALUE', 2.0),
        webhook_secret=secret,
        host=os.getenv('MNQ_HOST', '0.0.0.0').strip() or '0.0.0.0',
        port=_int('MNQ_PORT', 8788),
        ngrok_enabled=_bool('MNQ_NGROK', True),
        ngrok_authtoken=os.getenv('NGROK_AUTHTOKEN', '').strip(),
        data_dir=DATA_DIR,
    )
