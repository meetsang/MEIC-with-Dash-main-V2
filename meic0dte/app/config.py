import os

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
project_root = current_dir
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        project_root = current_dir
        break
    current_dir = os.path.dirname(current_dir)

# Generic Params
LOG_PATH = os.path.join(project_root, 'meic0dte')
ORDER_PARAMS_PATH = os.path.join(project_root, 'meic0dte')

STREAM_SYMBOLS = os.path.join(project_root, 'streaming', 'optsymbols.json')
AUTH_TOKEN = os.path.join(project_root, 'common', 'auth', 'token.json')

# Order Params
FILL_WAIT = 5

INDEX_SYMBOL = '$SPX'
OPTION_SYMBOL = 'SPXW'

STEP = 5
SPREAD_WIDTH_MIN = 25
SPREAD_WIDTH_MAX = 35


OTM_MIN = 5
OTM_MAX = 150

CREDIT_MAX_P = 1.85
CREDIT_MAX_C = 1.85
CREDIT_MIN = 0.90

STOP_PRCNT_P = 2.0
STOP_PRCNT_C = 2.0

# Entry thread: max seconds to wait for initial fills before handing off to stop_monitor
FILL_WAIT_MAX = 5

# After updating optsymbols.json, wait for streamer to subscribe and publish MQTT mids
STREAMER_QUOTE_WAIT = 5



OPEN_PRICE_ADJ = 0.05
STOP_OFFSET = 0.05
LIMIT_OFFSET = 0.1
# Software breach closes via limit at market; slippage vs designated stop uses this uplift, not broker limit.
SOFTWARE_BREACH_SLIPPAGE_UPLIFT = 1.0

STRK_CHK_MIN = 51
STRK_IDX_DIFF = 3

QUANTITY = 1
