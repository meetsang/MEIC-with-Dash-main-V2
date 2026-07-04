import os

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
project_root = current_dir
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        project_root = current_dir
        break
    current_dir = os.path.dirname(current_dir)

# Streaming MQTT Broker Params
MQTT_BROKER_ADDR = "localhost"
TOPIC_PREFIX = "SCHWAB/"          # Default; TastyTrade uses TASTYTRADE/ via broker_factory
INDEX_TOPIC = TOPIC_PREFIX + "SPX"
KILL_SWITCH_TOPIC = TOPIC_PREFIX + "MEIC_Close_All"
INDEX_SYMBOL = '$SPX'

STREAM_SYMBOLS = os.path.join(project_root, 'streaming', 'optsymbols.json')
SPX_TICK_DATA_PATH = os.path.join(project_root, 'streaming', 'spx_tick_data.csv')
VIX_TICK_DATA_PATH = os.path.join(project_root, 'streaming', 'vix_tick_data.csv')

# Slack Params
SLACK_WEBHOOK_URL = ''