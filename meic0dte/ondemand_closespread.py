import os
import sys

# Dynamically find the project root
current_dir = os.path.abspath(os.path.dirname(__file__))
while current_dir and current_dir != os.path.dirname(current_dir):
    if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
        if current_dir not in sys.path:
            sys.path.insert(0, current_dir)
        break
    current_dir = os.path.dirname(current_dir)
import app.config as config, app.utilities as util
from close import closespread
import threading,json

symbols_lock = threading.Lock()

opttype = "P"
lot = "01-45"
if opttype == "C":
    f_name = f"{lot}_od_close_call.log"
elif opttype == "P":
    f_name = f"{lot}_od_close_put.log"
log = util.get_logger(opttype,f_name)
file_path = f"{config.ORDER_PARAMS_PATH}/app/order_params.json"

log.info("Reading Order Params from the file")
try:
    with symbols_lock:
        with open(file_path, 'r') as file:
            data = json.load(file)
except Exception as e:
    log.info(f"ERROR Opening Order Params File - {e}")
log.info(data)

short_symbol= data[lot][opttype]['short_symbol']
long_symbol = data[lot][opttype]['long_symbol']
filled_quantity = data[lot][opttype]['filled_quantity']
short_leg_price = data[lot][opttype]['short_open_price']
long_leg_price = data[lot][opttype]['long_open_price']
filled_price = data[lot][opttype]['filled_price']
short_close_order_id = data[lot][opttype]['short_close_order_id']

log.info("Starting Closing Action")
close_spread_status = closespread.close_spread(short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,lot,log)
log.info(f"Close Spread Status : {close_spread_status}")
log.info("Closing Action Completed")
