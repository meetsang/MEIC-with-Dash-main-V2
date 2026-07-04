import os
from dotenv import load_dotenv

# Load .env from project root (two levels up from this file)
_here = os.path.abspath(os.path.dirname(__file__))
_env_path = os.path.join(_here, '..', '..', '.env')
load_dotenv(_env_path)

# Generic Params
PROD_AUTH_BASE_URL='https://api.schwabapi.com/v1'    
PROD_MARKET_BASE_URL='https://api.schwabapi.com/marketdata/v1'    
PROD_TRADER_BASE_URL='https://api.schwabapi.com/trader/v1'  

# PROD Account Params - loaded from .env
CLIENT_ID     = os.getenv('SCHWAB_CLIENT_ID', '')
CLIENT_SECRET = os.getenv('SCHWAB_CLIENT_SECRET', '')
P_ACCT        = os.getenv('SCHWAB_ACCT', '')

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
project_root = current_dir
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        project_root = current_dir
        break
    current_dir = os.path.dirname(current_dir)

# Token Params
TOKEN_FILE_PATH = os.path.join(project_root, 'common', 'auth', 'token.json')
BEARER_FILE_PATH = os.path.join(project_root, 'common', 'auth', 'bearer.py')

# Slack Params
SLACK_WEBHOOK_URL = ''
