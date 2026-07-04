import asyncio, datetime
import app.utilities as util
from close import longclose,shortclose,fillcheck
from datetime import datetime as dt


async def close_spread_task(short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,meic_close_queue,\
                            short_queue,long_queue,index_queue,short_orderid_queue,long_orderid_queue,\
                            short_stoplmt_rplc_queue,check_long_close_fill_queue,stop_close_spread_task_queue,\
                            short_close_price_queue,long_close_price_queue,lot,log):
    log.info("Starting Closing Spread Task")
    log.info("Allowing time for the stream task to put values in to the short, long and index queues")
    await asyncio.sleep(3)#Allowing time for the stream task to put values in to the short, long and index queues
    
    short_close_flag=False
    long_close_flag=False
    stop_close_spread_task = stop_close_spread_task_queue.queue[-1]
    count = 1 
    
    # Main async loop starts here
    while not stop_close_spread_task:
        
        t_time = util.central_time()
        stop_close_spread_task = stop_close_spread_task_queue.queue[-1]
        short_close_order_id = short_orderid_queue.queue[-1]
        check_long_close_fill_flag = check_long_close_fill_queue.queue[-1]

        #Put values in to the queue in case the stream sub task dint put any values on time.
        if short_queue.qsize() == 0 or long_queue.qsize() == 0:
            short_queue.put(float(short_leg_price))
            long_queue.put(float(long_leg_price))
        if index_queue.qsize() == 0:
            index_queue.put(10000.00)

        # Stop the program on market close  
        if t_time >= datetime.time(15,00,00):
            log.info("Times up")
            stop_close_spread_task_queue.put(True)
            while True:
                if util.write_close_params_to_file(short_symbol[-9],short_close_price_queue.queue[-1],long_close_price_queue.queue[-1],lot,log):
                    log.info("Close Order Params written to file successfully.")
                    break
                asyncio.sleep(1)
            return 1

        # Check order fill status every 2 minute in case the close order was stopped out
        if count%10==0 and not short_close_flag:
            fill_status,rem_qty = fillcheck.check_fill_status(short_close_order_id,"SHORT",short_close_price_queue,lot,log)
            # Accepted working order was filled
            if fill_status==1:
                short_close_flag=True
                log.info(f"SUCCESS - SHORT LEG CLOSED - Filled {filled_quantity} SHORT LEG")
            #Accepted order was later rejected for multiple reasons. So putting the original orderid back in to the queue
            if fill_status==2:
                log.info(f"Accepted working order was later rejected for multiple reasons. So putting the original {short_close_order_id} orderid back in to the queue")
                short_orderid_queue.put(short_orderid_queue.queue[-2])
                short_stoplmt_rplc_queue.put(False)
            # Partially Filled so checking order fill status again
            elif fill_status==4:
                log.info(f"Accepted short working order is Partially Filled, so checking order fill again")
                continue

        # Short Close Co-Routine
        if not short_close_flag:
            short_close_status = shortclose.short_close(short_symbol,filled_price,filled_quantity,short_leg_price,count,short_stoplmt_rplc_queue,\
                                                        meic_close_queue,short_queue,long_queue,index_queue,short_orderid_queue,\
                                                        short_close_price_queue,t_time,lot,log)
            if short_close_status == 1:
                short_close_flag=True
                log.info(f"SUCCESS - SHORT LEG CLOSED - Filled {filled_quantity} SHORT LEG")
        # Long Close Co-Routine
        if short_close_flag and not long_close_flag:
            long_close_status = longclose.long_close(long_symbol,filled_quantity,long_orderid_queue,long_queue,check_long_close_fill_queue,\
                                                        long_close_price_queue,check_long_close_fill_flag,lot,log)
            if long_close_status == 1:
                long_close_flag = True
                stop_close_spread_task_queue.put(True)
                log.info(f"SUCCESS - LONG LEG CLOSED - Filled {filled_quantity} LONG LEG")
                while True:  
                    if util.write_close_params_to_file(short_symbol[-9],short_close_price_queue.queue[-1],long_close_price_queue.queue[-1],lot,log):
                        log.info("Close Order Params written to file successfully.")
                        break
                    asyncio.sleep(1)
                return 1

        count+=1
        await asyncio.sleep(3)  # Simulate a longer operation