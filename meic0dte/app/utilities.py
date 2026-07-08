import logging, threading, json, datetime, os
from datetime import datetime as dt, time, timedelta, timezone

try:
    import meic0dte.app.config as config
except ModuleNotFoundError:
    import app.config as config
from common import config as cmn_config

file_lock = threading.Lock()

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


def _central_offset_hours(local_dt):
    dst_start, dst_end = _central_dst_bounds(local_dt.year)
    return CENTRAL_DST_OFFSET if dst_start <= local_dt < dst_end else CENTRAL_STD_OFFSET


def _central_offset_for_utc(utc_now: dt) -> int:
    dst_start_local, dst_end_local = _central_dst_bounds(utc_now.year)
    dst_start_utc = (dst_start_local - timedelta(hours=CENTRAL_STD_OFFSET)).replace(tzinfo=timezone.utc)
    dst_end_utc = (dst_end_local - timedelta(hours=CENTRAL_DST_OFFSET)).replace(tzinfo=timezone.utc)
    return CENTRAL_DST_OFFSET if dst_start_utc <= utc_now < dst_end_utc else CENTRAL_STD_OFFSET


def central_now():
    utc_now = dt.now(timezone.utc)
    offset = _central_offset_for_utc(utc_now)
    return (utc_now + timedelta(hours=offset)).replace(tzinfo=None)


def central_from_epoch(epoch: float) -> dt:
    """Convert Unix epoch to naive US/Central local time (same basis as central_now)."""
    utc_now = dt.fromtimestamp(epoch, tz=timezone.utc)
    offset = _central_offset_for_utc(utc_now)
    return (utc_now + timedelta(hours=offset)).replace(tzinfo=None)


def central_date():
    return central_now().date()


def central_time():
    return central_now().time()


def crossed_market_close(
    session_start: dt,
    now: dt | None = None,
    *,
    close_hour: int = 15,
    close_minute: int = 0,
) -> bool:
    """True when the session started before close and the clock has reached close.

    Starting the bot after 3 PM CT (e.g. for manual spread / dashboard) does not
    trigger shutdown — only a session that was running before close crosses it.
    """
    now = now or central_now()
    close = time(close_hour, close_minute)
    return session_start.time() < close and now.time() >= close

#=============================================================================================================================#

class TerminateRequest(Exception):
    def __init__(self,message):
        super().__init__(message)
        print(f"SCHWAB MEIC ERROR: {message}")

#=============================================================================================================================#

#Defining Individual Thread logger fundtion
def get_logger(name, log_file, level=logging.DEBUG):
    log_dir = os.path.join(config.LOG_PATH, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    handler = logging.FileHandler(os.path.join(log_dir, log_file), mode='w')
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)   
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    return logger

#=============================================================================================================================#

def get_lot_time():
    current_time = central_time()
     # Define time ranges and their corresponding lot values in a dictionary
    lot_times = {
        "11-00": (time(10, 59), time(11, 5)),
        "12-00": (time(11, 59), time(12, 5)),
        "12-30": (time(12, 29), time(12, 35)),
        "01-15": (time(13, 14), time(13, 20)),
        "01-45": (time(13, 44), time(13, 50)),
        "02-00": (time(13, 59), time(14, 5))
        }
    # Check each lot and time range, then call vertical.tranche if current time is in the range
    for item, (start, end) in lot_times.items():
        if start <= current_time <= end:
            lot = item
            return lot  # Exit once the appropriate tranche is found
    raise TerminateRequest(f"No Matching LOT Time found for this time {current_time}")
    
#=============================================================================================================================#

def utc_to_est(sysDT):    
    if getattr(sysDT, "tzinfo", None) is not None:
        sysDT = sysDT.astimezone(timezone.utc).replace(tzinfo=None)
    est_time = sysDT.strftime("%H:%M:%S")
    hour,minute,second = (int(x) for x in est_time.split(':'))
    tme = datetime.time(hour,minute,second)
    return tme

#=============================================================================================================================#

def create_option_symbol(short_strike,long_strike,opt_type):
    expiration_date = str(central_date().strftime("%y%m%d"))
    short_symbol = f"SPXW  {expiration_date}{opt_type}0{short_strike}000"
    long_symbol = f"SPXW  {expiration_date}{opt_type}0{long_strike}000"
    return short_symbol,long_symbol

#=============================================================================================================================#

def get_expiration_date(log):
    override = os.environ.get('MEIC_EXPIRY', '').strip()
    if override:
        if len(override) == 10 and '-' in override:
            override = dt.strptime(override, '%Y-%m-%d').strftime('%y%m%d')
        elif len(override) == 8 and '-' not in override:
            override = dt.strptime(override, '%Y%m%d').strftime('%y%m%d')
        log.info('Using MEIC_EXPIRY override: %s', override)
        return override

    expiration_date = central_date().strftime("%y%m%d")
    if os.environ.get('MEIC_FORCE_TRADE', '').lower() in ('1', 'true', 'yes'):
        log.info('MEIC_FORCE_TRADE set — skipping market-closed/FOMC expiry check.')
        return expiration_date

    # Check for Mrket closure or FOMC days.
    current_date = central_date().strftime("%y%m%d")
    if current_date in cmn_config.FOMC_DAY or current_date in cmn_config.MARKET_CLOSED or expiration_date in cmn_config.MARKET_CLOSED:
        log.info(f"Market is closed on {expiration_date} or {current_date} or today is a FOMC day, so no orders will be placed.")   
        raise TerminateRequest(f"Market is closed. No orders will be placed")
    else:
        return expiration_date

#=============================================================================================================================#

def get_bearer(log):
    symbols_lock = threading.Lock()
    # Acquire the lock
    file_path = config.AUTH_TOKEN
    try:
        with symbols_lock:
            with open(file_path, 'r') as file:
                data = json.load(file)
    except Exception as e:
        log.info(f"ERROR Opening Bearer file - {e}")

        # raise utility.TerminateRequest(f"ERROR Opening {lot} Bearer File - {e}")
    bearer = data['token']["access_token"]
    return bearer

#=============================================================================================================================#

def update_options_symbols(additional_symbols,lot,log):
    file_path = config.STREAM_SYMBOLS
    try:
        with file_lock:
            with open(file_path, 'r') as file:
                data = json.load(file)
    except Exception as e:
        log.info(f"ERROR Opening {lot} Options Symbol File - {e}")
        return False
    try:
        data["SYMBOLS"].extend(additional_symbols)
        data["SYMBOLS"] = list(set(data["SYMBOLS"]))
        with file_lock:
            with open(file_path, 'w') as file:
                json.dump(data, file, indent=2)
    except Exception as e:
        log.info(f"ERROR Writing to {lot} Options Symbol File - {e}")
        return False
    return True
    
#=============================================================================================================================#

def write_open_params_to_file(opt_type,open_order_id,short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,lot,log):
    symbols_lock = threading.Lock()
    # Acquire the lock
    file_path = f"{config.ORDER_PARAMS_PATH}/app/order_params.json"
    try:
        with symbols_lock:
            with open(file_path, 'r') as file:
                data = json.load(file)
    except Exception as e:
        log.info(f"ERROR Opening Order Params File - {e}")

        return False
    
    date_opened = central_date().strftime("%Y-%m-%d")
    time_opened = central_time().strftime("%H:%M")

    if lot not in data:
        data[lot] = {}
    if opt_type not in data[lot]:
        data[lot][opt_type] = {}
        
    data[lot][opt_type]['date_opened'] = date_opened
    data[lot][opt_type]['time_opened'] = time_opened
    data[lot][opt_type]['open_order_id'] = open_order_id
    data[lot][opt_type]['short_symbol'] = short_symbol
    data[lot][opt_type]['long_symbol'] = long_symbol      
    data[lot][opt_type]['short_open_price'] = short_leg_price
    data[lot][opt_type]['long_open_price'] = long_leg_price    
    data[lot][opt_type]['filled_quantity'] = int(filled_quantity)
    data[lot][opt_type]['filled_price'] = filled_price
    data[lot][opt_type]['short_close_order_id'] = short_close_order_id
    

    try:
        # Save the updated data back to the file
        with symbols_lock:
            with open(file_path, 'w') as file:
                json.dump(data, file, indent=2)
        log.info(f"{lot} lot {opt_type} OPEN Params Writem to Order Params File Successfully")
    except Exception as e:
        log.info(f"ERROR Writing OPEN Order Params to File - {e}")

        return False
    return True
#=============================================================================================================================#

def write_close_params_to_file(opt_type,short_close_price,long_close_price,lot,log):

    # Acquire the lock
    file_path = f"{config.ORDER_PARAMS_PATH}/app/order_params.json"
    try:
        with file_lock:
            with open(file_path, 'r') as file:
                data = json.load(file)
    except Exception as e:
        log.info(f"ERROR Opening Order Params File - {e}")
    
        return False
    
    data[lot][opt_type]['short_close_price'] = short_close_price
    data[lot][opt_type]['long_close_price'] = long_close_price    

    try:
        # Save the updated data back to the file
        with file_lock:
            with open(file_path, 'w') as file:
                json.dump(data, file, indent=2)
        log.info(f"{lot} lot {opt_type} CLOSE Params Writem to Order Params File Successfully")
    except Exception as e:
        log.info(f"ERROR Writing CLOSE Order Params to File - {e}")

        return False
    return True
#=============================================================================================================================#

