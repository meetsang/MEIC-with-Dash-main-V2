import requests, json, sys, time
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

import app.config as config, app.utilities as util, common.auth.config as auth_config

def get_quotes(bearer,quote_params,quote_type,lot,log):
    if quote_type == "IDX" or quote_type == "VIX":
        quotes_list = quotes(bearer,quote_params,lot,log)
    elif quote_type == "VERT":
        option_type,short_strike,long_strike = quote_params
        # log.info("Creating Option symbols")
        short_symbol,long_symbol = util.create_option_symbol(short_strike,long_strike,option_type)
        # log.info(f"Short Symbol: {short_symbol}, Long Symbol: {long_symbol}")    
        symbols = f"{short_symbol},{long_symbol}"
        quotes_list = quotes(bearer,symbols,lot,log)
        # If invalid symbols then return None for short and long price.
        if quotes_list == 1:
            log.info("Invalid Symbols so skipping the strike.")
            return None,None,short_symbol,long_symbol

    for symbol,quote in quotes_list.items():     
        if (symbol == config.INDEX_SYMBOL or symbol == "$VIX") and "quote" in quote:         
            index_price = float(quote['quote']['lastPrice'])
            return index_price
            
        if quote['symbol'] == short_symbol:
            short_price = float(quote['quote']['mark'])
            # log.info(f"Short Price: {short_price}")   
            
        elif quote['symbol'] == long_symbol:
            long_price = float(quote['quote']['mark'])
            # log.info(f"Long Price: {long_price}")
                
        else:
            log.info("Error: No Last value in Get Quotes Data")
            raise util.TerminateRequest(f"{lot} lot Get Quotes Error - No Last value in Get Quotes Data")
    return short_price,long_price,short_symbol,long_symbol

def quotes(bearer,symbols,lot,log):
    """
    Calls quotes API to provide quote details for equities, options, and mutual funds
    """
    base_url=auth_config.PROD_MARKET_BASE_URL   # URL for the API endpoint
    url = f"{base_url}/quotes?symbols={symbols}&fields=quote&indicative=false"

    done_flag = False
    while not done_flag:
        header = {"Authorization": f"Bearer {bearer}"}
        # Make API call for GET request
        try:
            response = requests.request("GET", url, headers=header)
        except Exception as e:
            # done_flag = True            
            util.TerminateRequest(f"{lot} lot Get Quotes Request Call Failed - {e}")
            time.sleep(5)
            continue

        if response.status_code == 200:
            data = json.loads(response.text)
            # log.info(data)
            if data is not None and not "errors" in data:     
                return data
            # Handle errors
            elif data is not None and "errors" in data :
                log.info(data)
                for error in data["errors"]:
                    msg = f"{lot} lot Get Quotes Error - {error}"
                    log.info(msg)
                    if "invalidSymbols" in error:
                        return 1
                    done_flag = True
                    raise util.TerminateRequest(msg) 
        elif response.status_code == 401:
            msg = f"Bearer Token expired. Getting New Token. {response.text}"
            log.info(msg)
            util.TerminateRequest(msg)
            bearer = util.get_bearer(log)
            continue       
        else:
            msg = f"{lot} lot Get Quotes API Request Error - {response.status_code} {response.text}"
            log.info(msg)
            done_flag = True
            raise util.TerminateRequest(msg)