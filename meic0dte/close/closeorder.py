from open import quotes, fillcheck
from app import utilities as util, config
from order import order, orderdetails
import time


def close_order(symbol,filled_quantity,close_order_id,lot,log):
    count = 0
    order_filled_flag = False
    check_fill_again = False


    while not order_filled_flag:
        count+=1
        if check_fill_again == False:
            quote_price = quotes.get_quotes(symbol,"SNGL",lot,log)
            if quote_price > 3:
                short_limit_price = round(quote_price,1) 
            else:
                short_limit_price = round(round((quote_price+0.05)/0.05)*0.05,2)
            log.info(f"Attempt {count} : Credit - {short_limit_price}")
            trade_action = "BUY_TO_CLOSE"
            order_spec = orderdetails.limit_order(trade_action,symbol,filled_quantity,short_limit_price,log)
            close_order_id = order.place_order(order_spec,lot,log)
            
            time.sleep(config.FILL_WAIT)     

        close_state,close_order = fillcheck.check_openfill(close_order_id,lot,log)
        log.info(f"Open Order Status: {close_order['orderId']} - {close_order['status']}")        
        if close_state == 0:
            log.info("Open Order Not Filled on time. CANCELLING ORDER.")
            cancel_state = order.cancel_order(close_order_id,lot,log)
            if cancel_state == 2:
                #If Cancel state is 2 then cancel order errored out, means it might have been filled or request erored out, so try fill status again.
                check_fill_again = True
            elif cancel_state == 1:
                # If cancel state is 1 order is Cancelled
                log.info("Confirming Cancel Status")
                time.sleep(1)
                cancel_confirm,close_order = fillcheck.check_openfill(close_order_id,lot,log)
                if cancel_confirm == 3:#order Cancelled Successfully
                    log.info("Order Cancelled Successfully.")
                    check_fill_again = False      
                elif cancel_confirm == 1:#Order was filled
                    log.info("Open Order Filled.")
                    order_filled_flag = True         
        elif close_state == 1:
            log.info("Open Order Filled.")
            order_filled_flag = True      
            return close_order
        elif close_state == 2 or close_state == 3:
            log.info("Open Order Rejected or Cancelled.")
            check_fill_again = False
        elif close_state == 4:
            log.info("Open Order Partially Filled.")
            check_fill_again = True


