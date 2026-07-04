import sys, asyncio, queue
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
from close import streamtask,closetask

def close_spread(short_close_order_id,short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,lot,log):
    log.info("Starting Closing Tasks")
    # Declare Order Id queues to keep track of order id's
    short_orderid_queue = queue.Queue()
    long_orderid_queue = queue.Queue()
    short_stoplmt_rplc_queue = queue.Queue()
    check_long_close_fill_queue = queue.Queue()
    stop_close_spread_task_queue = queue.Queue()
    short_close_price_queue = queue.Queue()
    long_close_price_queue = queue.Queue()
    meic_close_queue = queue.Queue()

    # Declare queues to store the streaming tick data
    short_queue = queue.Queue()
    long_queue = queue.Queue()
    index_queue = queue.Queue()

    # Put default values in to the queues
    short_orderid_queue.put(short_close_order_id)
    long_orderid_queue.put(1000) # Putting in some dummy value
    short_stoplmt_rplc_queue.put(False)
    check_long_close_fill_queue.put(False)
    stop_close_spread_task_queue.put(False)
    meic_close_queue.put(False)
    short_close_price_queue.put(0.00)
    long_close_price_queue.put(0.00)

    async def task_loop():
        close_spread_Status = await asyncio.gather(streamtask.stream_sub_task(short_symbol,long_symbol,short_queue,long_queue,index_queue,stop_close_spread_task_queue,meic_close_queue,lot,log),
                            closetask.close_spread_task(short_leg_price,long_leg_price,filled_price,filled_quantity,short_symbol,long_symbol,meic_close_queue,\
                                                short_queue,long_queue,index_queue,short_orderid_queue,long_orderid_queue,\
                                                short_stoplmt_rplc_queue,check_long_close_fill_queue,stop_close_spread_task_queue,\
                                                short_close_price_queue,long_close_price_queue,lot,log))
        for status in close_spread_Status:
            if status == 1:
                log.info("All legs Closed")
                return status
    # Run the event loop
    return asyncio.run(task_loop())


