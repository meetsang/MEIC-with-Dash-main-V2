import sys, os, json, requests, base64, time
_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _root not in sys.path:
    sys.path.insert(0, _root)
from common.auth import util
from common.auth import config

def refresh(log):
    token_path = config.TOKEN_FILE_PATH

    # Replace these placeholders with your actual values
    client_id = config.CLIENT_ID
    client_secret = config.CLIENT_SECRET
    redirect_uri = "https://localhost.com"

    # Base64 encode the client_id and client_secret
    auth = f'{client_id}:{client_secret}'
    base64_auth = base64.b64encode(auth.encode()).decode()

    try:
        with open(token_path, 'r') as f:
            token = json.load(f)
    except Exception as e:
        raise util.TerminateRequest(f"Error Opening and Reading Refresh Token in to JSON  - {e}")
    refresh_token = token['token']["refresh_token"]

    # URL and headers for the request
    url = 'https://api.schwabapi.com/v1/oauth/token'
    headers = {
        'Authorization': f'Basic {base64_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    # Data to be sent in the POST request
    data = {
        'grant_type': 'refresh_token',
        'refresh_token': refresh_token,
        'redirect_uri': redirect_uri
    }
    try:
        r = requests.post(url, headers=headers, data=data)
    except Exception as e:
        msg = f"ERROR posting refresh token request - {e}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    
    if r.status_code == 200:
        try:
            token_data = json.loads(r.text)
            wrapped_token = {
            'creation_timestamp': int(time.time()),
            'token': token_data
    }
            # data = r.json()
        except Exception as e:
            msg = f"ERROR parsing refresh token response to json - {e}"
            log.info(msg)
        # print(r.content)
        try:
            with open(token_path, 'w') as file:
                json.dump(wrapped_token, file)
        except Exception as e:
            msg = f"ERROR opening file and Writing to refresh token - {e}"
            log.info(msg)
            raise util.TerminateRequest(msg)
    else:
        msg = f"ERROR with refresh token Request - {r.content}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    
    try:
        with open(token_path, 'r') as f:
            token = json.load(f)
    except Exception as e:
        raise util.TerminateRequest(f"Error Opening and Reading Bearer Token in to JSON  - {e}")
    bearer = token['token']["access_token"]
    log.info("Access Token Refreshed")

    # Define the constant string
    constant_str = f'bearer = "{bearer}"'
    # Specify the file path where you want to save the .py file
    bearer_path = config.BEARER_FILE_PATH
    # Write the bearertoken to the bearer.py file
    try:
        with open(bearer_path, "w") as file:
            file.write(constant_str)
    except Exception as e:
        msg = f"ERROR opening Bearer file and Writing token - {e}"
        log.info(msg)
        raise util.TerminateRequest(msg)
    log.info(f"Bearer Token has been written to {bearer_path}")
    print(f"Bearer Token has been written to {bearer_path}")
    return bearer