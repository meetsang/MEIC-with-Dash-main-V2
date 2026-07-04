import time
from order import orderdetails,order
from app import config as app_config
from close import fillcheck

def long_close(long_symbol,filled_quantity,long_orderid_queue,long_queue,check_long_close_fill_queue,\
                long_close_price_queue,check_long_close_fill_flag,lot,log):
    
    action = "LONG CLOSE LMT"     
    log.info(action)        

    long_close_order_id = long_orderid_queue.queue[-1]
    current_long_price = long_queue.queue[-1]

    long_limit_price = round(round(current_long_price/0.05)*0.05,2)
    # If Current price is < 0.05 then do nothing, keep checking 
    if current_long_price < 0.05:
        log.info(f"current_long_price is < 0.05")
        return

    if check_long_close_fill_flag == False:             
        long_spec = orderdetails.limit_order("SELL_TO_CLOSE",long_symbol,filled_quantity,long_limit_price,log)
        long_close_order_id = order.place_order(long_spec,lot,log)
        long_orderid_queue.put(long_close_order_id)
        time.sleep(app_config.FILL_WAIT)

    fill_status,rem_qty = fillcheck.check_fill_status(long_close_order_id,"LONG",long_close_price_queue,lot,log)
    if fill_status == 1:#Filled
        return 1
    elif fill_status == 2:#Rejected. Accepted working order was later rejected for multiple reasons. So putting the original orderid back in to the queue
        log.info(f"Accepted working order was later rejected for multiple reasons. So putting the original {long_close_order_id} orderid back in to the queue")
        long_close_order_id = long_orderid_queue.queue[-2]
        long_orderid_queue.put(long_close_order_id)
        return
    #Partially Filled
    elif fill_status==4:
        log.info(f"Accepted long working order is Partially Filled, so checking order fill again")
        check_long_close_fill_queue.put(True)
        filled_quantity = rem_qty
        return rplc_order(long_symbol,filled_quantity,long_limit_price,long_close_order_id,long_close_price_queue,long_orderid_queue,lot,log)
    elif fill_status == 0:#Cancelled or Not Filled
        check_long_close_fill_queue.put(True)
        if current_long_price <= 0.05:
            time.sleep(297)
            return
        log.info(f"Long Close Order not filled.  Placing Long Close Replace Order")
        return rplc_order(long_symbol,filled_quantity,long_limit_price,long_close_order_id,long_close_price_queue,long_orderid_queue,lot,log)

def rplc_order(long_symbol,filled_quantity,long_limit_price,long_close_order_id,long_close_price_queue,long_orderid_queue,lot,log):
    long_spec = orderdetails.limit_order("SELL_TO_CLOSE",long_symbol,filled_quantity,long_limit_price,log)
    long_replace_order_id = order.replace_order(long_close_order_id,long_spec,lot,log)
    if long_replace_order_id == 3:#400 Error
        fillcheck.check_fill_status(long_close_order_id,"LONG",long_close_price_queue,lot,log)
        return 1
    else:
        long_orderid_queue.put(long_replace_order_id)            
        time.sleep(app_config.FILL_WAIT)
        return