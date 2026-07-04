import time
import app.config as app_config
from order import orderdetails,order
from close import fillcheck


def strike_rchd_close(short_symbol,filled_quantity,short_close_price_queue,short_orderid_queue,lot,log):
    short_close_order_id = short_orderid_queue.queue[-1]
    action = "SHORT CLOSE MKT"     
    log.info(action)             
    short_spec = orderdetails.market_order("BUY_TO_CLOSE",short_symbol,filled_quantity,log)
    short_replace_order_id = order.replace_order(short_close_order_id,short_spec,lot,log)
    # Order rejected with a reason that it is already filled
    if short_replace_order_id == 3:
        fillcheck.check_fill_status(short_close_order_id,"SHORT",short_close_price_queue,lot,log)
        return 1
    else:
        # short_close_order_id = short_replace_order_id
        short_orderid_queue.put(short_replace_order_id)
        time.sleep(app_config.FILL_WAIT)
        fill_status,rem_qty = fillcheck.check_fill_status(short_replace_order_id,"SHORT",short_close_price_queue,lot,log)
        # Filled
        if fill_status==1:
            return 1
        # Accepted working order was later rejected for multiple reasons. So putting the original orderid back in to the queue
        elif fill_status==2:
            log.info(f"Accepted working order was later rejected for multiple reasons. So putting the original {short_close_order_id} orderid back in to the queue")
            short_close_order_id = short_orderid_queue.queue[-2]
            short_orderid_queue.put(short_close_order_id)
            return
        #Partially Filled so checking order fill status again
        elif fill_status==4:
            log.info(f"Accepted short working order is Partially Filled, so checking order fill again")
            return
        return