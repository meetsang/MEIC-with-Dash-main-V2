import requests
import json

def get_overnight_prcntchng(bearer, symbol, lot, log):
    base_url = "https://api.schwabapi.com/marketdata/v1"
    url = f"{base_url}/quotes?symbols={symbol}&fields=quote&indicative=false"
    
    header = {"Authorization": f"Bearer {bearer}"}
    try:
        response = requests.get(url, headers=header)
    except Exception as e:
        log.info(f"get_overnight_prcntchng failed: {e}")
        raise
        
    if response.status_code == 200:
        data = response.json()
        if symbol in data and "quote" in data[symbol]:
            quote_data = data[symbol]["quote"]
            last_price = float(quote_data.get("lastPrice", 0.0))
            close_price = float(quote_data.get("closePrice", 0.0))
            percent_change = float(quote_data.get("netPercentChange", 0.0))
            return close_price, last_price, percent_change
        else:
            msg = f"No VIX quote data found in response: {data}"
            log.info(msg)
            raise ValueError(msg)
    else:
        msg = f"VIX quote API returned status code {response.status_code}: {response.text}"
        log.info(msg)
        raise ValueError(msg)
