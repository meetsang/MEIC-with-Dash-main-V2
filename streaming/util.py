import logging
import os
from schwab import auth
from datetime import datetime as dt, timedelta, timezone

from common.auth import config
from common.session_logs import STREAM_SCHWAB_BASE, new_session_log_path, relocate_legacy_log
#=============================================================================================================================#

CENTRAL_STD_OFFSET = -6
CENTRAL_DST_OFFSET = -5

def _nth_weekday_of_month(year, month, weekday, n):
    first_day = dt(year, month, 1)
    days_until_weekday = (weekday - first_day.weekday()) % 7
    return first_day + timedelta(days=days_until_weekday + 7 * (n - 1))

def _central_dst_bounds(year):
    dst_start = _nth_weekday_of_month(year, 3, 6, 2).replace(hour=2, minute=0, second=0, microsecond=0)
    dst_end = _nth_weekday_of_month(year, 11, 6, 1).replace(hour=2, minute=0, second=0, microsecond=0)
    return dst_start, dst_end

def central_now():
    utc_now = dt.now(timezone.utc)
    dst_start_local, dst_end_local = _central_dst_bounds(utc_now.year)
    dst_start_utc = (dst_start_local - timedelta(hours=CENTRAL_STD_OFFSET)).replace(tzinfo=timezone.utc)
    dst_end_utc = (dst_end_local - timedelta(hours=CENTRAL_DST_OFFSET)).replace(tzinfo=timezone.utc)
    offset = CENTRAL_DST_OFFSET if dst_start_utc <= utc_now < dst_end_utc else CENTRAL_STD_OFFSET
    return (utc_now + timedelta(hours=offset)).replace(tzinfo=None)

def central_time():
    return central_now().time()

#=============================================================================================================================#

def createClientConnection():
    token_path = config.TOKEN_FILE_PATH
    try:
        c = auth.client_from_token_file(token_path, config.CLIENT_ID,config.CLIENT_SECRET)  
    except Exception as e:
            print(f"createClientConnection Error - Either Client connection due to invalid token or wrong API Key issue occured- {e}")
            raise TerminateRequest(f"createClientConnection Error - Either Client connection due to invalid token or wrong API Key issue occured- {e}") 
    return c

#=============================================================================================================================#


class TerminateRequest(Exception):
    def __init__(self,message):
        super().__init__(message)
        print(f"SCHWAB STREAMING APP ERROR: {message}")


#=============================================================================================================================#

#Defining Individual Thread logger fundtion
def get_logger(name, log_base, level=logging.DEBUG):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    relocate_legacy_log(root, log_base)
    log_path = new_session_log_path(root, log_base, when=central_now())
    handler = logging.FileHandler(log_path, mode='w')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    logger.info('Streamer log: %s', log_path)
    return logger

#=============================================================================================================================#