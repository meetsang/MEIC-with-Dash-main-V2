import app.config as config, app.utilities as util
from order import orderdetails, order
import time


def place_stop(order_details,short_symbol,long_symbol,opt_type,lot,log):
    log.info("Order FILLED")      
    filled_price = order_details['price']
    filled_quantity = order_details['filledQuantity']
    log.info(f"{lot} SUCCESS ${filled_price} Filled {filled_quantity}")
    # utility.log_alert_notification(f"SUCCESS ${filled_price}",f"Filled{filled_quantity}")

    action = "SHORT CLOSE STOP LIMIT"    
    # Get Short Leg Price
    order_leg_collections= order_details['orderLegCollection']
    for leg in order_leg_collections:
        if leg['instruction'] == "SELL_TO_OPEN":
            short_leg_id = leg['legId']
    order_executions = order_details['orderActivityCollection'][0]
    for execution in order_executions['executionLegs']:
        if execution['legId'] == short_leg_id:
            short_leg_price = execution['price']
            
    action = "SHORT STOP LIMIT"   

   
    if opt_type == "C":
        stop_prcnt = config.STOP_PRCNT_C
    elif opt_type == "P":
        stop_prcnt = config.STOP_PRCNT_P
          

        
     # Calculate the STOP Price for the filled order        
    short_stop_price = round(round(((short_leg_price-0.10)*stop_prcnt)/0.05)*0.05,2)
    short_limit_price = round(short_stop_price+config.LIMIT_OFFSET,2)
    log.info(f"short_stop_price: {short_stop_price} , short_limit_price: {short_limit_price}")

    if short_stop_price >= 2.90:
        short_stop_price = round(short_stop_price,1)
        short_limit_price = round(short_limit_price,1)

    log.info(f"Placing {action} Order")
    short_stop_spec = orderdetails.stop_limit_order(short_symbol,filled_quantity,short_stop_price,short_limit_price,log)    
    stop_order_id = order.place_order(short_stop_spec,lot,log)    
    log.info(f"{action} Order placed Successfully.")

    log.info("Updating option symbols to the optionsymbols file to restart streaming")
    while True:
        if util.update_options_symbols([short_symbol, long_symbol],lot,log):
            break
        time.sleep(1)
    log.info("Option Symbol File Updated Successfully")
    
    # Get Long Leg Price
    order_leg_collections= order_details['orderLegCollection']
    for leg in order_leg_collections:
        if leg['instruction'] == "BUY_TO_OPEN":
            long_leg_id = leg['legId']
            order_executions = order_details['orderActivityCollection'][0]
            for execution in order_executions['executionLegs']:
                if execution['legId'] == long_leg_id:
                    long_leg_price = execution['price']
                    filled_price = round((short_leg_price-long_leg_price),2)
            break
        else:
            long_leg_price = 0.05
    
    close_params = stop_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity 
    return close_params



    
