import app.config as config, app.utilities as util, open.quotes as quotes
import open.check_vix as vix
import time, threading, json

def get_open_spread_price(opt_type,lot,log):

    spread_width_min,spread_width_max = get_spread_width(lot,opt_type)

    otm_min, otm_max = get_otm_minmax(lot)

    credit_max,credit_min = get_credit_minmax(lot,opt_type)

    step = config.STEP
    bearer = util.get_bearer(log)

    spread_flag = False
    count = 1
    while not spread_flag:
        log.info("Acquiring Index Quote")
        spx_price = round(quotes.get_quotes(bearer,config.INDEX_SYMBOL,"IDX",lot,log)/5)*5  
        log.info(f"Index Price: {spx_price}")
        log.info(f"Getting {opt_type} Spread legs and Credit Price - attempt {count}")
        for spread_width in range(spread_width_min,spread_width_max,step):
            log.info(f"{opt_type} Attempting Spread Width {spread_width} to get suitable Price") 
            for otm in range(otm_min,otm_max,step):
                if opt_type == "C":
                    SP_sell = str(spx_price+otm)
                    SP_buy = str(spx_price+otm+spread_width)
                    params_for_quotes = opt_type,SP_sell,SP_buy
                    short_price,long_price,short_symbol,long_symbol = quotes.get_quotes(bearer,params_for_quotes,"VERT",lot,log)   
                if opt_type == "P":
                    SP_sell = str(spx_price-otm)
                    SP_buy = str(spx_price-otm-spread_width)
                    params_for_quotes = opt_type,SP_sell,SP_buy
                    short_price,long_price,short_symbol,long_symbol = quotes.get_quotes(bearer,params_for_quotes,"VERT",lot,log)   
                
                # If the short or long symbol is invalid, then skip the strike to move to next size.
                if short_price is None or long_price is None:
                    continue
                # If total credit is below the min credit, exit the loop
                credit = short_price-long_price
                if credit < config.CREDIT_MIN:
                    break
                rounded_credit = round(credit/0.05)*0.05
                #Adjust the logical operator to select the upper or lower value for the rounded credit.
                if rounded_credit > credit:
                    rounded_credit-=config.OPEN_PRICE_ADJ
                spread_credit = round(rounded_credit,2)  
                log.info(f"{short_symbol} - {short_price} | {long_symbol} - {long_price} = {spread_credit}")
                if (credit_min <= spread_credit <= credit_max):
                    # Check if the long symbol is already shorted as part of another lot
                    log.info("Check if the long symbol is already shorted as part of another lot")
                    if check_long_short(short_symbol,long_symbol,opt_type,log,lot):
                        continue
                    else:
                        log.info("No Long-Short Found. Continue to Place Order.")
                        spread_flag = True
                        return short_symbol,long_symbol,spread_credit   
                    
        if count == 10:
            log.info(f"ERROR - {lot} {opt_type} Max Attempts REached. No suitable Credit Price found. Spread not opened.")
            raise util.TerminateRequest(f"ERROR - {lot} {opt_type} was not opened. No suitable Credit Price found.")
        count+=1
        time.sleep(0.5)     

def get_spread_width(lot,opt_type):
   
    spread_width_min = config.SPREAD_WIDTH_MIN
    spread_width_max = config.SPREAD_WIDTH_MAX

    return spread_width_min,spread_width_max

def get_otm_minmax(lot):
  
    otm_min = config.OTM_MIN
    otm_max = config.OTM_MAX
    return otm_min,otm_max

def get_credit_minmax(lot,opt_type):
   
    if opt_type == "C":
        credit_max = config.CREDIT_MAX_C
    elif opt_type == "P":
        credit_max = config.CREDIT_MAX_P
    credit_min = config.CREDIT_MIN

    return credit_max,credit_min

def check_long_short(short_symbol,long_symbol,opt_type,log,lot):
    # Acquire the lock
    symbols_lock = threading.Lock()
    
    file_path = f"{config.ORDER_PARAMS_PATH}/app/order_params.json"
    try:
        with symbols_lock:
            with open(file_path, 'r') as file:
                data = json.load(file)
    except Exception as e:
        log.info(f"ERROR Opening Order Params File - {e}")
        log.info(f"ERROR Opening {lot} Order Params File - {e}")

    for orderlot, options in data.items():
        for option_type, details in options.items():
            if opt_type == option_type:
                if long_symbol == details["short_symbol"]:
                    log.info(f"Long Symbol is already shorted in a {orderlot} lot. So trying again.")
                    return True
                if short_symbol == details["long_symbol"]:
                    log.info(f"Long Symbol is already shorted in a {orderlot} lot. So trying again.")
                    return True
    return False