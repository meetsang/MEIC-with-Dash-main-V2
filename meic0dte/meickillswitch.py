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
import paho.mqtt.publish as publish
from streaming import config

# Set the value for MT Trades Kill Switch
try:
    publish.single(config.KILL_SWITCH_TOPIC, "True", retain = True, hostname=config.MQTT_BROKER_ADDR)
    print("Kill Switch set to True. All MEIC trades will close now")
except Exception as e:
    print(f"ERROR Publishing Message to MQTT Broker Topic MEIC_CLOSE_ALL- {e}")

