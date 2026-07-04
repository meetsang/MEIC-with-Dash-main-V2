import os, sys
_dir = os.path.abspath(os.path.dirname(__file__))
_root = os.path.dirname(_dir)
if _root not in sys.path:
    sys.path.insert(0, _root)

import paho.mqtt.publish as publish
from schwab.streaming import StreamClient
from streaming import config
from streaming import util
from common.session_logs import STREAM_SCHWAB_BASE

import asyncio, time, json, threading, csv
from datetime import datetime as dt, timezone

count = 0
option_symbol_file_path = config.STREAM_SYMBOLS

def client_func(slog):
    client = util.createClientConnection()
    try:
        slog.info("Establishing Stream Client connection")
        #Establishing Stream Client connection
        stream_client = StreamClient(client)
    except Exception as e:
        slog.info(f"ERROR Establishing Stream Client connection - {e}")
                    
    slog.info("Stream Client connection Created")

    #Set the value for MT Trades Kill Switch
    try:
        publish.single(config.KILL_SWITCH_TOPIC, "False", retain=True, hostname=config.MQTT_BROKER_ADDR)
        slog.info("MT and MEIC Trades Close All Topic is set to False.")
    except Exception as e:
        slog.info(f"ERROR Publishing Message to MQTT Broker Topic MT_Close_All or MEIC_Close_All - {e}")


    # Calling Streaming function
    stream_func(stream_client, slog)

    slog.info("Logging out of the Stream")
    try:
        asyncio.run(stream_client.logout())
    except Exception as e:
        slog.info(f"ERROR Logging Out- {e}")

    return

def stream_func(stream_client, slog):
    # Define the symbols_lock
    symbols_lock = threading.Lock()
    handlers_registered = False  # Track if handlers have been registered

    def get_optsymbols():
        while True:
            try:
                with symbols_lock:
                    with open(option_symbol_file_path, 'r') as f:
                        optsymbols = json.load(f)
                        return set(optsymbols['SYMBOLS'])
            except Exception as e:
                slog.info(f"ERROR Opening Option Symbol File - {e}")

                time.sleep(5)
                continue

    # Stream Message Handler
    def message_handler(message):
        global count
        count += 1
        quotes = message['content']

        if count % 30 == 0:
            slog.info(quotes)

        for quote in quotes:
            if 'LAST_PRICE' in quote and quote['LAST_PRICE'] is not None:
                topic_name = quote['key']
                current_price = quote['LAST_PRICE']
                if topic_name == config.INDEX_SYMBOL:
                    topic_name = "SPX"
                tick = str(f"{current_price}")
                try:
                    publish.single(config.TOPIC_PREFIX + topic_name, tick, hostname=config.MQTT_BROKER_ADDR)
                except Exception as e:
                    slog.info(f"ERROR Publishing Message to MQTT Broker Topic {topic_name} - {e}")

            elif 'CLOSE_PRICE' in quote and quote['CLOSE_PRICE'] is not None:
                slog.info(f"TICK: {quote}")
                if quote['key'] == config.INDEX_SYMBOL:
                    chart_time = quote['CHART_TIME_MILLIS'] / 1000
                    open_price = quote['OPEN_PRICE']
                    close_price = quote['CLOSE_PRICE']
                    high_price = quote['HIGH_PRICE']
                    low_price = quote['LOW_PRICE']
                    try:
                        tick_time = dt.fromtimestamp(chart_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        with open(config.SPX_TICK_DATA_PATH, mode='a', newline='') as file:
                            writer = csv.writer(file)
                            writer.writerow([tick_time,open_price,close_price,high_price,low_price])
                        slog.info(f"SPX value {close_price} stored at {tick_time}")
                    except Exception as e:
                        slog.error(f"Error writing SPX tick data to CSV: {e}")
                elif quote['key'] == "$VIX":
                    chart_time = quote['CHART_TIME_MILLIS'] / 1000
                    open_price = quote['OPEN_PRICE']
                    close_price = quote['CLOSE_PRICE']
                    high_price = quote['HIGH_PRICE']
                    low_price = quote['LOW_PRICE']
                    try:
                        tick_time = dt.fromtimestamp(chart_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                        with open(config.VIX_TICK_DATA_PATH, mode='a', newline='') as file:
                            writer = csv.writer(file)
                            writer.writerow([tick_time,open_price,close_price,high_price,low_price])
                        slog.info(f"VIX value {close_price} stored at {tick_time}")
                    except Exception as e:
                        slog.error(f"Error writing VIX tick data to CSV: {e}")
            else:
                continue

    async def reconnect_stream(stream_client):
        """Handles the reconnection of the stream on error, especially after a 1011 error."""
        slog.info("Reconnecting stream...")

        await read_stream()  # Reconnect by calling the stream function again

    async def read_stream():
        nonlocal handlers_registered
        stop_flag = False
            
        slog.info("Getting Initial Symbol List")
        initial_symbols = get_optsymbols()
        slog.info(f"Initial Symbols List: {initial_symbols}")

        try:
            slog.info("Logging in and Setting QOS Level")
            await stream_client.login()
        except Exception as e:
            slog.info(f"ERROR Logging in to the Stream Client- {e}")

            time.sleep(10)
            await reconnect_stream(stream_client)

        if not handlers_registered:
            try:
                slog.info("Registering Message Handlers")
                stream_client.add_level_one_equity_handler(message_handler)
                stream_client.add_level_one_option_handler(message_handler)
                stream_client.add_chart_equity_handler(message_handler)
                # stream_client.add_level_one_futures_handler(message_handler)
                # stream_client.add_level_one_futures_options_handler(message_handler)
                handlers_registered = True  # Set the flag to avoid duplicate registration
            except Exception as e:
                slog.info(f"ERROR Registering Handlers- {e}")

                await stream_client.logout()
                time.sleep(10)
                await reconnect_stream(stream_client)

        try:
            slog.info("Subscribing to the option Symbols Stream")
            await stream_client.level_one_equity_subs([config.INDEX_SYMBOL],fields=[stream_client.LevelOneEquityFields(0), stream_client.LevelOneEquityFields(3)])
            await stream_client.level_one_option_subs(initial_symbols,fields=[stream_client.LevelOneOptionFields(0), stream_client.LevelOneOptionFields(4)])
            await stream_client.chart_equity_subs([config.INDEX_SYMBOL,"$VIX"])
            # await stream_client.level_one_futures_subs([config.INDEX_SYMBOL],fields=[stream_client.LevelOneEquityFields(0), stream_client.LevelOneEquityFields(3)])
            # await stream_client.level_one_futures_options_subs(initial_symbols,fields=[stream_client.LevelOneOptionFields(0), stream_client.LevelOneOptionFields(4)])
            # await stream_client.chart_futures_subs([config.INDEX_SYMBOL])
        except Exception as e:
            slog.info(f"ERROR creating Option Subscription- {e}")

            await stream_client.logout()
            time.sleep(10)
            await reconnect_stream(stream_client)

        slog.info("Starting Message Event Loop")
        while not stop_flag:
            current_time = util.central_time()
            current_symbols = get_optsymbols()
            updated_symbols = current_symbols - initial_symbols

            if current_time >= dt.strptime('15:00', '%H:%M').time():
                slog.info("Times Up - Stopping Message Event Loop")
                stop_flag = True

                with open(option_symbol_file_path, 'w') as file:
                    json.dump({"SYMBOLS": [config.INDEX_SYMBOL]}, file, indent=2)
                slog.info("Options Symbol File is Updated with empty list")

                with open(config.SPX_TICK_DATA_PATH, 'w', newline='') as csvfile:
                    pass  # Nothing needs to be written, as this will clear the file.
                slog.info("SPX CSV tick data file is cleared")

                with open(config.VIX_TICK_DATA_PATH, 'w', newline='') as csvfile:
                    pass  # Nothing needs to be written, as this will clear the file.
                slog.info("VIX CSV tick data file is cleared")
                return
            elif updated_symbols:
                slog.info(f"Symbol list updated: {updated_symbols}. Adding new symbols to the stream")
                try:
                    await stream_client.level_one_option_add(updated_symbols,fields=[stream_client.LevelOneOptionFields(0), stream_client.LevelOneOptionFields(4)])
                    time.sleep(1)
                    initial_symbols = current_symbols
                except Exception as e:
                    slog.info("Error adding new symbols to the Stream. Restarting the stream")

                    # await stream_client.logout()
                    await reconnect_stream(stream_client)
            else:
                try:
                    await stream_client.handle_message()
                except Exception as e:
                    if "1011" in str(e) or "no close frame received" in str(e) or "ping timeout" in str(e):
                        slog.info(f"ERROR 1011 - WebSocket Disconnected: {e}")
                        time.sleep(10)
                        await reconnect_stream(stream_client)
                    else:
                        slog.info(f"ERROR Handling Message- {e}")

                        await stream_client.logout()
                        time.sleep(10)
                        await reconnect_stream(stream_client)

            time.sleep(0.5)

    asyncio.run(read_stream())

if __name__ == "__main__":
    slog = util.get_logger("stream_pub", STREAM_SCHWAB_BASE)
    client_func(slog)
