from open import spreadprice
from order import orderdetails, order


def open_spread(count,opt_type,quantity,lot,log):    
    short_symbol,long_symbol,credit = spreadprice.get_open_spread_price(opt_type,lot,log)
    log.info(f"Attempt {count} : Credit - {credit}")
    order_details = orderdetails.open_order(short_symbol,long_symbol,quantity,credit,log)
    log.info("Placing Open Order")
    open_order_id = order.place_order(order_details,lot,log)
    return short_symbol,long_symbol,open_order_id