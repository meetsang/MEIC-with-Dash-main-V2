import app.config as config, app.utilities as util
from common.auth import config as auth_config
import json, requests, time
from urllib.parse import urlparse

def place_order(order_details,lot,log):
    base_url=auth_config.PROD_TRADER_BASE_URL   # URL for the API endpoint
    bearer = util.get_bearer(log)  

    log.info("Creating Place Order Request Payload")
    url = f"{base_url}/accounts/{auth_config.P_ACCT}/orders"
    # Add parameters and header information
    header = {"Authorization": f"Bearer {bearer}"}
    try:
        response = requests.request("POST", url, json=order_details, headers=header)
    except Exception as e:
        msg = f"{lot} lot Place Order Request Call Failed - {e}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    log.info(f"Request : {response.request.body}")

    if response.status_code == 201:
        order_link = response.headers.get('Location')
        if order_link: 
            log.info(order_link)
            order_id = urlparse(order_link).path.split('/')[-1]    
            log.info(f"Order Id: {order_id}")
            return order_id     
        else:
            log.info(f"{lot} - Order created but no Location header found.")
            raise util.TerminateRequest(f"{lot} - Order created but no Location header found.") 
    else:
        # Handle errors
        log.info(f"Error: {response.status_code}-{response.text}")
        raise util.TerminateRequest(f"{lot} lot Place Order API Error - {response.status_code}-{response.text}")
    
        
def replace_order(order_id,order_details,lot,log):   
    log.info("Placing Replace Order")
    base_url=auth_config.PROD_TRADER_BASE_URL   # URL for the API endpoint
    accountid = auth_config.P_ACCT
    bearer = util.get_bearer(log)   

    log.info("Creating Replace Order Request Payload")
    url = f"{base_url}/accounts/{accountid}/orders/{order_id}"
    header = {
        "Authorization": f"Bearer {bearer}"
        }
    try:
        response = requests.request("PUT", url, json=order_details, headers=header)
    except Exception as e:
        msg = f"{lot} lot Replace Order Request Call Failed - {e}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    log.info(f"Request : {response.request.body}")
    
    if response.status_code == 201:
        order_link = response.headers.get('Location')
        if order_link: 
            log.info(order_link)
            order_id = urlparse(order_link).path.split('/')[-1]    
            log.info(f"Order Id: {order_id}")
            return order_id     
        else:
            log.info(f"{lot} - Order created but no Location header found.")
            raise util.TerminateRequest(f"{lot} - Replace Order created but no Location header found.")
    elif response.status_code == 400:
        log.info(f"Error: {response.status_code}-{response.text}")
        return 3
    else:
        # Handle errors
        log.info(f"Error: {response.status_code}-{response.text}")
        raise util.TerminateRequest(f"{lot} lot Replace Order API Error - {response.status_code}-{response.text}")


def cancel_order(order_id,lot,log):
    log.info("Placing Cancel Order")           
    base_url=auth_config.PROD_TRADER_BASE_URL   # URL for the API endpoint
    accountid = auth_config.P_ACCT
    bearer = util.get_bearer(log)  

    log.info("Creating Cancel Order Request Payload")
    url = f"{base_url}/accounts/{accountid}/orders/{order_id}"
    header = {
        "Authorization": f"Bearer {bearer}"
        }
    try:
        response = requests.request("DELETE", url, headers=header)
    except Exception as e:
        msg = f"{lot} lot Cancel Order Request Call Failed - {e}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    log.info(f"Request : {response.request.url}")

    if response.status_code == 200:
        log.info(f"{response.status_code} - Order Cancelled.")  
        return 1
    elif response.status_code == 400:
        if "Order in state FILLED cannot be canceled" in response.text:
            log.info(f"Order already Filled Check Fill Status again-{response.text}")
            return 2
        else:
            log.info(f"Error: {response.status_code}-{response.text}")
            alert.alert_notification(f"{lot} CNCL Order",f"Error: {response.status_code}-{response.text}")
            time.sleep(10)
            return 2
    else:
        # Handle errors
        log.info(f"Error: {response.status_code}-{response.text}")
        alert.alert_notification(f"{lot} CNCL Order",f"Error: {response.status_code}-{response.text}")
        time.sleep(10)
        return 2

