import datetime,time
import app.config as app_config
from order import orderdetails,order
from close import fillcheck, strikecheck

def short_close(short_symbol,filled_price,filled_quantity,short_leg_price,count,short_stoplmt_rplc_queue,meic_close_queue,\
                      short_queue,long_queue,index_queue,short_orderid_queue,short_close_price_queue,t_time,lot,log):
                              
    current_short_price = short_queue.queue[-1]
    current_long_price = long_queue.queue[-1]
    index_price = index_queue.queue[-1]  
    short_stoplmt_rplc_flag = short_stoplmt_rplc_queue.queue[-1]
    meic_close_flag = meic_close_queue.queue[-1]
    opt_type = short_symbol[-9]

    if opt_type == "C":
        stop_prcnt = app_config.STOP_PRCNT_C
    elif opt_type == "P":
        stop_prcnt = app_config.STOP_PRCNT_P
         
        
    # Update Stop Check price based on the short rplc flag
    spread_stop_price = round(round((filled_price*stop_prcnt)/0.05)*0.05,2)  
    stop_price = round(round((short_leg_price*stop_prcnt+0.20)/0.05)*0.05,2) # Add offset to avoid placing duplicate orders
    if short_stoplmt_rplc_flag:
        stop_price = round(spread_stop_price+0.20,2) # Add offset to avoid placing duplicate orders

    current_spread_price = round(current_short_price-current_long_price,2)
    short_limit_price = round(round(current_short_price/0.05)*0.05,2)

    if count%3==0:
        log.info(f"Short: {current_short_price}, Long: {current_long_price}, Spread: {current_spread_price}, Stop: {stop_price}, Index: {index_price}, meic_close_flag: {meic_close_flag}")
            
    # Close the short strike with Market order if the underlying is 5pt from the short strike at 3:45PM         
    if t_time >= datetime.time(14,app_config.STRK_CHK_MIN,00):
        log.info(f"checking Strike Price")
        short_underlying = index_price
        short_strike = float(short_symbol[-7:-3])
        short_contract_type = short_symbol[-9]
        if short_contract_type == 'C':
            if short_strike - short_underlying <= app_config.STRK_IDX_DIFF:
                log.info(f"Current index price {index_price} is less than {app_config.STRK_IDX_DIFF} point from the Strike Price {short_strike}. Executing Market Close order.")
                short_status = strikecheck.strike_rchd_close(short_symbol,filled_quantity,short_close_price_queue,short_orderid_queue,lot,log)
                if short_status == 1:
                    return 1
        if short_contract_type == 'P':
            if short_underlying - short_strike  <= app_config.STRK_IDX_DIFF and index_price > 0.0:
                log.info(f"Current index price {index_price} is less than {app_config.STRK_IDX_DIFF} point from the Strike Price {short_strike}. Executing Market Close order.")
                short_status = strikecheck.strike_rchd_close(short_symbol,filled_quantity,short_close_price_queue,short_orderid_queue,lot,log)
                if short_status == 1:
                    return 1  
    
    # Close the spread with limit order if the stop price has reached.  
    if current_spread_price >= stop_price or meic_close_flag == "True":      
        action = "LMT_CLS"
        log.info(action)
        log.info(f"Current Spread Price: {current_spread_price} exceeded Spread Stop Price:{stop_price} and short order is not filled. So Replacing Short Close order")
        return execute_rplc_order(action,short_symbol,filled_quantity,short_limit_price,spread_stop_price,short_close_price_queue,short_orderid_queue,\
                                            short_stoplmt_rplc_queue,lot,log)

    if current_long_price <= 0.05 and short_stoplmt_rplc_flag == False:            
        # diff = long_leg_price - current_long_price  
        action = "STPLMT_RPLC"
        log.info(action)          
        log.info("Current Long leg price is <= 0.05 Short Stop limit order Replaced to Spread Stop")
        return execute_rplc_order(action,short_symbol,filled_quantity,short_limit_price,spread_stop_price,short_close_price_queue,\
                                    short_orderid_queue,short_stoplmt_rplc_queue,lot,log)


def execute_rplc_order(action,short_symbol,filled_quantity,short_limit_price,spread_stop_price,short_close_price_queue,\
                       short_orderid_queue,short_stoplmt_rplc_queue,lot,log):
    
    short_close_order_id = short_orderid_queue.queue[-1]
    fill_status,rem_qty = fillcheck.check_fill_status(short_close_order_id,"SHORT",short_close_price_queue,lot,log)
    # Filled
    if fill_status==1:
        log.info(f"Accepted working order was Filled.")
        return 1
    #Accepted order was later rejected for multiple reasons. So putting the original orderid back in to the queue
    elif fill_status==2:
        log.info(f"Accepted working order was later rejected for multiple reasons. So putting the original {short_orderid_queue.queue[-2]} orderid back in to the queue")
        short_orderid_queue.put(short_orderid_queue.queue[-2])
        short_stoplmt_rplc_queue.put(False)
        return
    #Partially Filled
    elif fill_status==4:
        log.info(f"Accepted working order is Partially Filled, so replacing order for remaining quantity")
        filled_quantity = rem_qty
        action == "LMT_CLS"
        log.info(f"Short Limit Price : {short_limit_price}")   
        short_spec = orderdetails.market_order("BUY_TO_CLOSE",short_symbol,filled_quantity,log)
        return rplc_order(action,short_close_order_id,short_spec,short_close_price_queue,short_orderid_queue,short_stoplmt_rplc_queue,lot,log)
    # Accepted working order not filled so Replacing order
    elif fill_status==0:     
        if action == "STPLMT_RPLC":
            short_stop = spread_stop_price
            short_limit_price = round(short_stop+app_config.LIMIT_OFFSET,1)
            if short_stop >= 2.85:
                short_stop = round(short_stop,1)
                short_limit_price = round(short_limit_price,1)
            log.info(f"Current Short Stop Price : {short_stop}, Short Limit Price : {short_limit_price}")                
            short_spec = orderdetails.stop_limit_order(short_symbol,filled_quantity,short_stop,short_limit_price,log)
        elif action == "LMT_CLS":
            log.info(f"Short Limit Price : {short_limit_price}")  
            if short_limit_price >= 3.0: 
                short_limit_price = round(short_limit_price,1)
            short_spec = orderdetails.limit_order("BUY_TO_CLOSE",short_symbol,filled_quantity,short_limit_price,log)
    
        return rplc_order(action,short_close_order_id,short_spec,short_close_price_queue,short_orderid_queue,short_stoplmt_rplc_queue,lot,log)
        
            # time.sleep(5)
            # fill_status = fillcheck.check_fill_status(short_close_order_id,"SHORT",short_close_price_queue,lot,log)
            # # Accepted replace order was later rejected for multiple reasons. So putting the original orderid back in to the queue
            # if fill_status==2:
            #     short_orderid_queue.put(short_close_order_id)
            # else:
            #     short_orderid_queue.put(short_replace_order_id)
            #     short_stoplmt_rplc_queue.put(True)
            
            # short_orderid_queue.put(short_replace_order_id)
            # short_stoplmt_rplc_queue.put(True)

def rplc_order(action,short_close_order_id,short_spec,short_close_price_queue,short_orderid_queue,short_stoplmt_rplc_queue,lot,log):
    short_replace_order_id = order.replace_order(short_close_order_id,short_spec,lot,log)
    # In case of 401 error or 200 with error in it
    if short_replace_order_id == 1 or short_replace_order_id == 2:
        log.info("Short Replace Order Failed with an Error")
        return 
    #In case of 400 and order is already filled
    elif short_replace_order_id == 3:
        fillcheck.check_fill_status(short_close_order_id,"SHORT",short_close_price_queue,lot,log)
        return 1
    else:
        if action == "LMT_CLS":
            short_orderid_queue.put(short_replace_order_id)
            time.sleep(app_config.FILL_WAIT)
            return
        elif action == "STPLMT_RPLC":
            short_orderid_queue.put(short_replace_order_id)
            short_stoplmt_rplc_queue.put(True)
            return
              