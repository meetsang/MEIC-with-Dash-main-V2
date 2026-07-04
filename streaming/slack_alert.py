import json, sys, random, requests

from streaming import config

def customizer():    
    #Generating random hex color code
    hex_number = random.randint(1118481, 16777215)
    hex_number = str(hex(hex_number))
    hex_number = '#' + hex_number[2:]

    return hex_number

def alert_notification(title, message):
    url = config.SLACK_WEBHOOK_URL
    if not url:
        return
    message = message
    title = title
    slack_data = {
        "username": "schwab_autotrader",
        "icon_emoji": ":computer:",
        "channel": "#schwab-streaming",
        "attachments": [
            {
                "color": hex_number,
                "fields": [
                    {
                        "title": title,
                        "value": message,
                        "short": "false",
                    }
                ]
            }
        ]
    }
    byte_length = str(sys.getsizeof(slack_data))
    headers = {'Content-Type': "application/json", 'Content-Length': byte_length}
    response = requests.post(url, data=json.dumps(slack_data), headers=headers)
    if response.status_code != 200:
        # raise Exception(response.status_code, response.text)
        print("Slack Error Occured - {}").format(response.content)