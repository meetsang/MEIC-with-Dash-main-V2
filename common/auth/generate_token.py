import rauth, webbrowser, requests, json, base64
import config
from urllib.parse import unquote, urlparse,parse_qs
import time

token_file_path = config.TOKEN_FILE_PATH

"""Allows user authorization for the sample application with OAuth 1"""  
trdstn = rauth.OAuth2Service(
            name="trdstn",
            client_id = config.CLIENT_ID,
            client_secret = config.CLIENT_SECRET,
            access_token_url="https://api.schwabapi.com/v1/oauth/token",
            authorize_url="https://api.schwabapi.com/v1/oauth/authorize",
            base_url=config.PROD_AUTH_BASE_URL
            )


def get_token(): 
    # Step 1: Get authorize url 
    authorize_url = trdstn.get_authorize_url(
        response_type="code",
        redirect_uri="https://127.0.0.1:8080/callback",
        audience="https://api.schwabapi.com/v1",
        state="STATE",
        acope = "api"
        # scope="openid offline_access MarketData ReadAccount Trade OptionSpreads"
    )
    print(authorize_url)
    webbrowser.open(authorize_url)

    code_url = input("Please accept agreement and enter verification code from browser: ")
    parsed_url = urlparse(code_url)
    query_params = parse_qs(parsed_url.query)
    code = query_params.get('code', [None])[0]
    decoded_code = unquote(code)
    print(decoded_code)

    # Replace these placeholders with your actual values
    client_id = trdstn.client_id
    client_secret = trdstn.client_secret
    authorization_code = decoded_code
    redirect_uri = "https://127.0.0.1:8080/callback"

    # Base64 encode the client_id and client_secret
    auth = f'{client_id}:{client_secret}'
    base64_auth = base64.b64encode(auth.encode()).decode()

    # URL and headers for the request
    url = 'https://api.schwabapi.com/v1/oauth/token'
    headers = {
        'Authorization': f'Basic {base64_auth}',
        'Content-Type': 'application/x-www-form-urlencoded'
    }

    # Data to be sent in the POST request
    data = {
        'grant_type': 'authorization_code',
        'code': authorization_code,
        'redirect_uri': redirect_uri
    }

    # Make the POST request
    response = requests.post(url, headers=headers, data=data)

    # # # # Step 3: Generate Access Token that would be used to create a session for each request
    # header = {"Content-Type": "application/x-www-form-urlencoded"}
    # token_data={
    #         "grant_type": "authorization_code",
    #         "client_id": config.CLIENT_ID,
    #         "client_secret": config.CLIENT_SECRET,
    #         "code": decoded_code,
    #         "redirect_uri": "https://localhost.com"}
    # response = requests.post(trdstn.access_token_url, headers=header, data=token_data)
    token_data = response.json()
    wrapped_token = {
            'creation_timestamp': int(time.time()),
            'token': token_data
    }
    print(wrapped_token)

    with open(token_file_path, 'w') as file:
        json.dump(wrapped_token, file)
 

if __name__ == "__main__":
    get_token()
