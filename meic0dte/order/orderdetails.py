import app.config as config

def open_order(short_symbol,long_symbol,quantity,credit,log):
    log.info("Creating Open Order Details")
    order_detail = {
                    "orderType": "NET_CREDIT",
                    "session": "NORMAL",
                    "price": str(credit),
                    "duration": "DAY",
                    "orderStrategyType": "SINGLE",
                    "orderLegCollection": [
                    {
                        "instruction": "BUY_TO_OPEN",
                        "quantity": str(quantity),
                        "instrument": {
                        "symbol": long_symbol,
                        "assetType": "OPTION"
                        }
                    },
                    {
                        "instruction": "SELL_TO_OPEN",
                        "quantity": str(quantity),
                        "instrument": {
                        "symbol": short_symbol,
                        "assetType": "OPTION"
                        }
                    }
                    ]
                    }
    log.info(order_detail)
    return order_detail

def stop_limit_order(symbol,quantity,stop,limit,log):  
    log.info("Creating Stop Limit Order Details")
    order_detail = { 
                        "complexOrderStrategyType": "NONE", 
                        "orderType": "STOP_LIMIT", 
                        "session": "NORMAL", 
                        "price": str(limit),
                        "stopPrice": str(stop),
                        "duration": "DAY", 
                        "orderStrategyType": "SINGLE", 
                        "orderLegCollection": [ 
                        { 
                            "instruction": "BUY_TO_CLOSE", 
                            "quantity": str(quantity), 
                            "instrument": { 
                            "symbol": symbol, 
                            "assetType": "OPTION" 
                            } 
                        } 
                        ] 
                    } 
    log.info(order_detail)
    return order_detail

def limit_order(tradeaction,symbol,quantity,limit,log):  
    log.info("Creating Limit Order Details")
    order_detail =  { 
                        "complexOrderStrategyType": "NONE", 
                        "orderType": "LIMIT", 
                        "session": "NORMAL", 
                        "price": str(limit),
                        "duration": "DAY", 
                        "orderStrategyType": "SINGLE", 
                        "orderLegCollection": [ 
                        { 
                            "instruction": tradeaction, 
                            "quantity": str(quantity), 
                            "instrument": { 
                            "symbol": symbol, 
                            "assetType": "OPTION" 
                            } 
                        } 
                        ] 
                    } 
    log.info(order_detail)
    return order_detail

def market_order(tradeaction,symbol,quantity,log):  
    log.info("Creating Market Order Details")
    order_detail = { 
                        "complexOrderStrategyType": "NONE", 
                        "orderType": "MARKET", 
                        "session": "NORMAL", 
                        "duration": "DAY", 
                        "orderStrategyType": "SINGLE", 
                        "orderLegCollection": [ 
                        { 
                            "instruction": tradeaction, 
                            "quantity": str(quantity), 
                            "instrument": { 
                            "symbol": symbol, 
                            "assetType": "OPTION" 
                            } 
                        } 
                        ] 
                    } 
    log.info(order_detail)
    return order_detail