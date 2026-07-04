# import sys, os
# # Dynamically find the project root
# current_dir = os.path.abspath(os.path.dirname(__file__))
# while current_dir and current_dir != os.path.dirname(current_dir):
#     if os.path.exists(os.path.join(current_dir, 'meic0dte')) or os.path.exists(os.path.join(current_dir, 'streaming')):
#         if current_dir not in sys.path:
#             sys.path.insert(0, current_dir)
#         break
#     current_dir = os.path.dirname(current_dir)
# from datetime import datetime as dt
# from app import utilities as util
# import common.auth.config as auth_config
# import requests,json
# log = util.get_logger("level", f"test.log")
# bearer = util.get_bearer(log) 

# base_url=auth_config.PROD_TRADER_BASE_URL   # URL for the API endpoint
# url = f"{base_url}/accounts/{auth_config.N_ACCT}/orders/1004935898624"

# header = {"Authorization": f"Bearer {bearer}"}
# # Make API call for GET request
# try:
#         response = requests.request("GET", url, headers=header)
# except Exception as e:
#         # done_flag = True            
#         util.TerminateRequest(f" Get Quotes Request Call Failed - {e}")

# log.info(f"Response Status Code: {response.text}")
# if response.status_code == 200:
#         data = json.loads(response.text)
#         print(data)
import time
from datetime import datetime as dt
import datetime
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from meic0dte.app.utilities import central_date
# Given symbol
symbol = "SPY   251119P00676000"
# exp_date = dt.strptime(symbol[6:12], "%y%m%d").date()
# today = dt.today().date()
# difference = (exp_date - today).days

# print(f"Expiration date: {exp_date}")
# print(f"Today's date: {today}")
# print(f"Days to expiration: {difference} days")
# opt_type = symbol[12]
# strike = symbol[15:18]
# opt_date = dt.strptime(symbol[6:12], "%y%m%d").date()
# today = dt.today().date()
# dte = (opt_date - today).days
# print(strike,opt_type,dte)
dte = 3
opt_type = "P"
strike = 676
if dte == 0:
    expiration_date = str(central_date().strftime("%y%m%d"))
else:
    today = central_date()
    exp_date = (today + datetime.timedelta(days=dte))
    if exp_date.strftime('%A') == "Saturday":
        dte+=2
    elif exp_date.strftime('%A') == "Sunday":
        dte+=3
    expiration_date = (today + datetime.timedelta(days=dte)).strftime("%y%m%d")
symbol = f"SPY   {expiration_date}{opt_type}00{strike}000"
print(symbol)   