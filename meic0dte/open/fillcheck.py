import app.config as config, app.utilities as util, common.auth.config as auth_config
import json, requests,time

def check_openfill(order_id,lot,log):
    log.info("Checking Order Status")
    accountid = auth_config.P_ACCT
    base_url= auth_config.PROD_TRADER_BASE_URL

    # URL for the API endpoint
    url = f"{base_url}/accounts/{accountid}/orders/{order_id}"
    
    done_flag = False
    while not done_flag:
        bearer = util.get_bearer(log)
        header = {"Authorization": f"Bearer {bearer}"}
        # Make API call for GET request
        try:
            response = requests.request("GET", url, headers=header)
        except Exception as e:
            util.TerminateRequest(f"{lot} lot Get Order Status Request Call Failed - {e}")
            time.sleep(5)
            continue

        if response.status_code == 200:
                data = json.loads(response.text)
                # log.info(data)
                if "errors" in data :
                    errors = data["errors"]
                    msg = f"{lot} lot Get Order Status {response.status_code} Error - {errors}"
                    log.info(msg)
                    done_flag = True
                    raise util.TerminateRequest(msg)   
                elif str(data['orderId']) == str(order_id):
                    if data['status'] == "FILLED":
                        log.info(f"Order: {data}")
                        return 1,data
                    elif data['status'] == "REJECTED":
                        log.info(f"Order: {data}")
                        return 2,data
                    elif data['status'] == "CANCELED":
                        log.info(f"Order: {data}")
                        return 3,data
                    elif data['status'] == "WORKING":
                        if data['filledQuantity'] == 0:# Order NOT FILLED
                            return 0,data
                        elif data['remainingQuantity'] != 0:# Order PARTIALLY FILLED
                            log.info(f"Order: {data}")
                            return 4,data
        elif response.status_code == 401:
            msg = f"Bearer Token expired. Getting New Token. {response.status_code}-{response.text}"
            log.info(msg)
            util.TerminateRequest(msg)
            time.sleep(20)
            continue
        elif response.status_code == 429:
            if "Too many requests" in response.text:
                msg = f"{response.status_code}-{response.text}"
                log.info(msg)
                time.sleep(20)
                continue
        elif response.status_code == 404:
            data = json.loads(response.text)
            errors = data["errors"]
            msg = f"{lot} lot Get Order Status {response.status_code} Error - {errors}"
            log.info(msg)
            done_flag = True
            raise util.TerminateRequest(msg)
        elif response.status_code == 503 or response.status_code == 504:
            data = json.loads(response.text)
            errors = data["fault"]["faultstring"]
            msg = f"{lot} lot Get Order Status {response.status_code} Error - {errors}"
            log.info(msg)
            util.TerminateRequest(msg)
            time.sleep(5)
            continue
        elif response.status_code == 599:
            msg = f"{lot} lot Get Order Status 599Error - {response.status_code} - {response.text}"
            log.info(msg)
            util.TerminateRequest(msg)
            time.sleep(5)
            continue
        else:
            # Handle errors
            msg = f"{lot} lot Get Order Status API Error - {response.status_code}-{response.text}"
            log.info(msg)
            done_flag = True
            raise util.TerminateRequest(msg)