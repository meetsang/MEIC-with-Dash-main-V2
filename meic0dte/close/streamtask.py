import sys, asyncio
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
import streaming.config as stream_config
import paho.mqtt.client as mqtt

async def stream_sub_task(short_symbol,long_symbol,short_queue,long_queue,index_queue,stop_close_spread_task_queue,meic_close_queue,lot,log):  
    log.info("Starting Streaming Subscription Task")
    stop_close_spread_task = stop_close_spread_task_queue.queue[-1]
    client = mqtt.Client()
    try:
        client.connect(stream_config.MQTT_BROKER_ADDR, 1883, 60)
    except Exception as e:
        log.info(f"ERROR Opening {lot} MQTT client Connection - {e}")
        log.info(f"ERROR Opening {lot} MQTT client Connection - {e}")
    
    # Define the callback function for when a message is received
    def on_message(client, userdata, msg):
        # print(msg.payload.decode())
        if msg.topic == stream_config.INDEX_TOPIC:
            index_queue.put(float(msg.payload.decode()))
            # print(f"Index: {msg.payload.decode()}")
            if index_queue.qsize()> 1:
                index_queue.get()
        elif msg.topic == stream_config.TOPIC_PREFIX + short_symbol:
            short_queue.put(float(msg.payload.decode()))
            # print(f"Short: {msg.payload.decode()}")
            if short_queue.qsize() > 1:
                short_queue.get()
        elif msg.topic == stream_config.TOPIC_PREFIX + long_symbol:
            long_queue.put(float(msg.payload.decode()))
            # print(f"Long: {msg.payload.decode()}")
            if long_queue.qsize() > 1:
                long_queue.get()
        elif msg.topic == stream_config.KILL_SWITCH_TOPIC:
            meic_close_queue.put(msg.payload.decode()) 

    # Set the on_message callback only once     
    log.info("Registering client on-message function.")            
    client.on_message = on_message     
    client.loop_start()

    while not stop_close_spread_task:
        stop_close_spread_task = stop_close_spread_task_queue.queue[-1]
        
        try:
            client.subscribe([(stream_config.TOPIC_PREFIX + short_symbol, 0), (stream_config.TOPIC_PREFIX + long_symbol, 0), (stream_config.INDEX_TOPIC, 0), (stream_config.KILL_SWITCH_TOPIC, 0)])
        except Exception as e:
            log.info(f"ERROR Subscribing to MQTT TOPICS - {e}")
            log.info(f"{lot} ERROR Subscribing to MQTT TOPICS - {e}")
        # client.loop_read()
        # client.loop_forever()

        # simulate a shorter operation
        await asyncio.sleep(1)
        # time.sleep(1)
    log.info("Stopping Stream Sub Task")
    client.loop_stop()
    client.disconnect()
    return 1
