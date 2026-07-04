"""TastyTrade configuration loaded from environment."""
import os
from dotenv import load_dotenv

_here = os.path.abspath(os.path.dirname(__file__))
_env_path = os.path.join(_here, '..', '.env')
load_dotenv(_env_path)

BROKER = os.getenv('BROKER', 'schwab').lower().strip()
PAPER_MODE = os.getenv('PAPER_MODE', 'false').lower() in ('1', 'true', 'yes')

TT_CLIENT_SECRET = os.getenv('TT_CLIENT_SECRET', '')
TT_REFRESH_TOKEN = os.getenv('TT_REFRESH_TOKEN', '')
TT_ACCOUNT_NUMBER = os.getenv('TT_ACCOUNT_NUMBER', '')
TT_IS_TEST = os.getenv('TT_IS_TEST', 'false').lower() in ('1', 'true', 'yes')
TASTYWARE_API_KEY = os.getenv('TASTYWARE_API_KEY', '')

from common import trades_layout

TRADES_ACTIVE_DIR = os.getenv('TRADES_ACTIVE_DIR', trades_layout.MEIC_ACTIVE)
TRADES_CLOSED_DIR = os.getenv('TRADES_CLOSED_DIR', trades_layout.MEIC_HISTORY)
MANUAL_SPREAD_ACTIVE_DIR = os.getenv(
    'MANUAL_SPREAD_ACTIVE_DIR', trades_layout.MANUAL_ACTIVE
)
MANUAL_SPREAD_CLOSED_DIR = os.getenv(
    'MANUAL_SPREAD_CLOSED_DIR', trades_layout.MANUAL_HISTORY
)
TRADES_OPS_DIR = os.getenv('TRADES_OPS_DIR', 'trades')
