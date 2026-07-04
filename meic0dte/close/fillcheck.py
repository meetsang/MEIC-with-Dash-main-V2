from open import fillcheck

# Check fill status of the working order
def check_fill_status(order_id,side,close_price_queue,lot,log):
    open_state,order = fillcheck.check_openfill(order_id,lot,log) 
     
    #Filled     
    if open_state == 1:            
        order_status = order['status'] 
        order_executions = order['orderActivityCollection'][0]
        for execution in order_executions['executionLegs']:
            fill_price = execution['price']
        if side == "SHORT":
            log.info(f"SHORT LEG Fill Price - {fill_price}")
            log.info(f"Close Short Order Status for {order_id}: {order_status}")
            close_price_queue.put(fill_price)
        elif side == "LONG":
            log.info(f"LONG LEG Fill Price - {fill_price}")
            log.info(f"Close Long Order Status for {order_id}: {order_status}")
            close_price_queue.put(fill_price)
        return 1,None
    #Rejected
    elif open_state == 2:
        order_status = order['status'] 
        order_description = order['statusDescription']
        log.info(f"Close Order Status for {order_id}: {order_status}- {order_description}")
        return 2,None
    #Cancelled
    elif open_state == 3:
        order_status = order['status'] 
        order_description = order['statusDescription']
        log.info(f"Close Order Status for {order_id}: {order_status}- {order_description}")
        return 3,None
    #Partially Filled
    elif open_state == 4:
        order_status = "PARTIALLY FILLED" 
        order_description = "PARTIALLY FILLED"
        log.info(f"Close Order Status for {order_id}: {order_status}- {order_description}")
        return 4,order['remainingQuantity']
    #Not Filled
    else:
        order_status = order['status'] 
        log.info(f"Close Order Status for {order_id}: {order_status}")
        return 0,None