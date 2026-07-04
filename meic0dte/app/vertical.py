import app.config as config, app.utilities as util
from open import openspread, fillcheck, fillaction
from order import order
from close import closespread

from datetime import datetime as dt
import time,threading,logging

def tranche(lot):

    print(f"{lot} MEIC Tranche Started - Session Begins")

    #Creating Thread List for Call and Put side
    threads = []
    opt_type = ["P","C"]

    try:      
        for item in opt_type:
            if item == "C":
                f_name = f"{lot}_call.log"
            elif item == "P":
                f_name = f"{lot}_put.log"
            log = util.get_logger(item,f_name)
            t = threading.Thread(target=vertical_spread, args=(lot,item,log))
            threads.append(t)

        # Start the threads
        for t in threads:
            t.start()
            time.sleep(5)

        # Wait for all threads to complete
        for t in threads:
            t.join()

    except Exception as e:
        print(f"An unhandled exception occurred: {type(e).__name__} - {e}")
        message = f"{e}"
        raise Exception(message)
    

    print(f"{lot} Tranche Ended - Session Ends")

# Define the Sprad Function
def vertical_spread(lot,opt_type,log):        

    expiration_date = util.get_expiration_date(log)
    log.info(f"Expiration date - {expiration_date}")
    quantity = config.QUANTITY

    # Set spread quantity based on the time and type
    # if opt_type == "C":
    #     quantity = config.CALL[lot]
    # elif opt_type == "P":
    #     quantity = config.PUT[lot]
    
    count = 0
    order_filled_flag = False
    check_open_fill_again = False
    open_order_id = None
    while not order_filled_flag:
        count+=1
        if check_open_fill_again == False:
            log.info("OPENING NEW ORDER")              
            short_symbol,long_symbol,open_order_id = openspread.open_spread(count,opt_type,quantity,lot,log)    
            time.sleep(config.FILL_WAIT)     

        # short_symbol = "SPXW 240426P5060"
        # long_symbol = "SPXW 240426P5035"
        # open_order_id = "1053826545"
        # Check fill status for the open order, if filled, calls the close spread function.
        open_state,open_order = fillcheck.check_openfill(open_order_id,lot,log)
        log.info(f"Open Order Status: {open_order['orderId']} - {open_order['status']}")        
        if open_state == 0:
            log.info("Open Order Not Filled on time. CANCELLING ORDER.")
            cancel_state = order.cancel_order(open_order_id,lot,log)
            if cancel_state == 2:
                #If Cancel state is 2 then cancel order errored out, means it might have been filled or request erored out, so try fill status again.
                check_open_fill_again = True
            elif cancel_state == 1:
                # If cancel state is 1 order is Cancelled
                log.info("Confirming Cancel Status")
                time.sleep(1)
                cancel_confirm,open_order = fillcheck.check_openfill(open_order_id,lot,log)
                if cancel_confirm == 3:#order Cancelled Successfully
                    log.info(f"Order Cancelled Successfully.") 
                    check_open_fill_again = False      
                elif cancel_confirm == 1:#Order was filled
                    log.info("Open Order Filled.")
                    order_filled_flag = True 
        elif open_state == 1:
            log.info("Open Order Filled.")
            order_filled_flag = True        
        elif open_state == 2 or open_state == 3:
            log.info("Open Order Rejected or Cancelled.")
            check_open_fill_again = False
        elif open_state == 4:
            log.info("Open Order Partially Filled.")
            time.sleep(5)
            check_open_fill_again = True

    #Placin Stop Limit Order        
    close_params = fillaction.place_stop(open_order,short_symbol,long_symbol,opt_type,lot,log)
    short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity = close_params
    log.info("Writing Order Parameter to the file")
    util.write_open_params_to_file(opt_type,open_order_id,short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,lot,log)
    # Wait for Streaming to restart and recevice the tick data for all the symbols.
    log.info("Waiting for the Streaming app to restart,pick up the updated symbol list, and receive tick data for all contracts")
    time.sleep(5)

    log.info(f"Starting Closing Action")
    close_spread_status = closespread.close_spread(short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,lot,log)
    if close_spread_status == 1:
        log.info(f"{lot} Ended - Session Ended.")   

        return
            
    log.info(f"ERROR - {lot} {opt_type} spread was not opened")
    raise util.TerminateRequest(f"{lot} {opt_type} OPEN FAILURE - NOT OPENED {quantity} Quanity")
