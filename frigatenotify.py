import atexit
import datetime
import json
import logging
import os
import re
import requests
import threading
import sched
import time
import yaml

import paho.mqtt.client as mqtt
from flask import Flask, request, jsonify, render_template, Response
from requests.exceptions import HTTPError, Timeout, ConnectionError

def exit_handler():
    logger.info("Frigate Notify is exiting.")

def send_healthcheck_ping():
    global last_ping_time
    current_time = datetime.datetime.now()
    if last_ping_time is None or (current_time - last_ping_time) >= datetime.timedelta(hours=1):
        api_url = f"https://hc-ping.com/{healthchecks_config['uuid']}"
        try:
            response = requests.get(api_url)
            response.raise_for_status()  # This will check for HTTP errors
            last_ping_time = current_time  # Update the last ping time
        except requests.RequestException as e:
            logger.error(f'An error occurred: {e}')

# Call this function at the appropriate place in your script
send_healthcheck_ping()

def send_pushover_notification(
    token, user, message, 
    ttl=None, attachment=None, html=None, sound=None, 
    timestamp=None, title=None, url=None, url_title=None, **kwargs):
    """
    Sends a notification via the Pushover API.

    Parameters:
        token (str): Your Pushover application token.
        user (str): The user/group key of the user/group to send the notification to.
        message (str): The message to send.
        ttl (int, optional): Time To Live in seconds. Defaults to None.
        attachment (str, optional): Path to JPEG attachment. Defaults to None.
        html (int, optional): 1 to enable HTML formatting, 0 to disable. Defaults to None.
        sound (str, optional): The name of one of the sounds supported by device clients. Defaults to None.
        timestamp (int, optional): A Unix timestamp of your message's date and time to display to the user. Defaults to None.
        title (str, optional): The title of the message. Defaults to None.
        url (str, optional): A supplementary URL to show with your message. Defaults to None.
        url_title (str, optional): A title for your supplementary URL. Defaults to None.
        **kwargs: Other optional parameters supported by the Pushover API.
    
    Returns:
        dict: The response from the Pushover API.
    """
    payload = {
        "token": token,
        "user": user,
        "message": message,
        "title": title,
        "url": url,
        "url_title": url_title,
        "sound": sound,
        "ttl": ttl,
    }

    # Filter out None values from payload
    payload = {k: v for k, v in payload.items() if v is not None}

    files = None
    if attachment is not None:
        files = {"attachment": ("thumbnail.jpg", attachment, "image/jpeg")}

    response = requests.post("https://api.pushover.net/1/messages.json", data=payload, files=files)
    return response.json()

# Example usage:
# response = send_pushover_notification(
#     token='your-app-token',
#     user='your-user-key',
#     message='Hello, World!',
#     ttl=60,
#     title='Optional Title',
#     attachment='path_to_image.jpg'
# )
# print(response)

def validate_config(config):
    errors = []

    # Validate MQTT section
    mqtt = config.get('mqtt', {})
    if not isinstance(mqtt.get('username'), str):
        errors.append("MQTT username should be a string.")
    if not isinstance(mqtt.get('password'), str):
        errors.append("MQTT password should be a string.")
    if not re.match(r'^[\w.-]+$', mqtt.get('host', '')):
        errors.append("MQTT host should be a valid FQDN or IP address.")
    if not isinstance(mqtt.get('port'), int):
        errors.append("MQTT port should be an integer.")
    if not re.match(r'^[\w/]+$', mqtt.get('topic', '')):
        errors.append("MQTT topic should be formatted as an MQTT topic.")
    if not re.match(r'^[\w/]+$', mqtt.get('alert_topic', '')):
        errors.append("MQTT topic should be formatted as an MQTT topic.")    

    # Validate Pushover section
    pushover = config.get('pushover', {})
    if not isinstance(pushover.get('api_key'), str):
        errors.append("Pushover api_key should be a string.")
    if not isinstance(pushover.get('user_key'), str):
        errors.append("Pushover user_key should be a string.")
    
    # Validate Healthchecks section
    healthchecks = config.get('healthchecks', {})
    if not isinstance(pushover.get('uuid'), str):
        errors.append("Healthchecks uuid should be a string.")

    # Validate Frigate Server section
    frigate_server = config.get('frigate_server', {})
    if not re.match(r'https?://[^\s]+', frigate_server.get('url', '')):
        errors.append("Frigate Server host should be a valid FQDN or IP address.")

    # Validate Web Server section
    web_server = config.get('web_server', {})
    if not re.match(r'https?://[^\s]+', web_server.get('url', '')):
        errors.append("Web Server URL should be a valid HTTPS URL.")

    # Validate Logging section
    if config.get('logging_level') not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        errors.append("Logging level should be a valid Python logging level.")
    if not re.match (r'^[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*$', logging.get('log_file')):
        errors.append("Log file should be a valid file name.")
    if not isinstance(config.get('log_to_screen'), bool):
        errors.append("'log_to_screen' should be a boolean value (True/False).")

    #Validate other fields
    if not isinstance(config.get('cooldown_period'), int):
        errors.append("Cooldown period should be an integer.")

    if errors:
        print("Configuration errors detected:")
        for error in errors:
            print(f"  - {error}")
        exit(1)
    else:
        return True
    
def load_config(config_file='/config/config.yaml'):
    try:
        with open(config_file, 'r') as f:
            return yaml.safe_load(f)

        if not validate_config(config):
            exit(1)

        return config
    
    except FileNotFoundError as e:
        logger.error(f"Configuration file not found: {e}")
        exit(1)
    except yaml.YAMLError as e:
        logger.error(f"Error in configuration file: {e}")
        exit(1)

def connect_to_mqtt():
    backoff_time = 1  # in seconds
    max_backoff_time = 60  # in seconds
    
    while True:
        try:
            client = mqtt.Client()
            client.on_connect = on_connect
            client.on_message = on_message
            
            # Enable Paho logging
            mqtt.MQTT_LOG_INFO = logging.INFO
            client.enable_logger()
            
            client.username_pw_set(mqtt_config['username'], mqtt_config['password'])
            client.connect(mqtt_config['host'], mqtt_config['port'], 60)

            client.reconnect_delay_set(min_delay=1, max_delay=120)  
            
            client.loop_forever()
            # Removed the break statement here

        except mqtt.ClientException as e:
            logger.error(f"MQTT Client Exception: {e}")
        except ConnectionRefusedError as e:
            logger.error(f"Connection Refused: {e}")
        except TimeoutError as e:  # Adding a new specific exception
            logger.error(f"Connection Timed Out: {e}")
        except Exception as e:
            logger.exception(f"An unexpected error occurred: {e}")

        # Incremental backoff
        time.sleep(backoff_time)
        backoff_time = min(backoff_time * 2, max_backoff_time)

def on_disconnect(client, userdata, rc):
    if rc != 0:
        logger.warning(f"Unexpected MQTT disconnection. Will auto-reconnect. Error code: {rc}")

def on_connect(client, userdata, flags, rc):
    if rc == 0:
        logger.info(f"Successfully connected to MQTT broker with return code {rc}")
    else:
        logger.warning(f"Connected with result code {rc}")
    client.subscribe(mqtt_config['topic'])

def on_message(client, userdata, msg):
    payload = json.loads(msg.payload)
    event_type = payload["type"]
    event_data = payload["after"]
    event_id = event_data["id"]
    label = payload['after']['label'].capitalize()
    camera = payload['after']['camera'].capitalize()
    timestamp = datetime.datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")
    
    current_time = datetime.datetime.now()
    send_healthcheck_ping()

    if event_type in ["new", "update"]:
        entered_zones = event_data.get("entered_zones", [])

        # If entered_zones is not empty, process the event
        if entered_zones:
            camera_label_combo = f"{event_data['camera']}_{event_data['label']}"
            last_alert_time = cooldown_dict.get(camera_label_combo, None)

            if (not last_alert_time) or (current_time - last_alert_time).total_seconds() >= cooldown_period:
                cooldown_dict[camera_label_combo] = current_time  # Update the last alert time
                
                if event_id not in processed_events:
                    processed_events.add(event_id)
           
                    thumbnail_url = f"{frigate_server}/api/events/{event_id}/thumbnail.jpg"
                    try:
                        thumbnail_response = requests.get(thumbnail_url, timeout=10)
                        thumbnail_response.raise_for_status()  # Raise HTTPError for bad responses (4xx and 5xx)
                        thumbnail_data = thumbnail_response.content
                    except requests.exceptions.RequestException as e:
                        logger.error(f"Failed to download snapshot due to network error: {e}")
                    except requests.exceptions.HTTPError as e:
                        logger.error(f"Failed to download snapshot. HTTP Error: {e}")

                    logger.info(f"Sending notification for {label} on {camera} camera.")
                    logger.debug(f"Event Data: {event_data}")
                    mqtt.single(
                        topic=mqtt_config['alert_topic'],
                        payload="trigger",
                        hostname=mqtt_config['host'],
                        port=mqtt_config['port'],
                        auth={'username': mqtt_config['username'], 'password': mqtt_config['password']}
                    )
                    message = f"{label} detected on {camera} camera at {timestamp}."
                    response = send_pushover_notification(
                        token=pushover_config['api_key'],
                        user=pushover_config['user_key'],
                        message=message,
                        url=f"{web_server}/event/{event_id}",
                        attachment=thumbnail_data,
                        title=f"{camera} camera alert.",
                        ttl=172800,
                        url_title="View Snapshot and Clip",
                        sound="gamelan"
                    )
                    print(response)
                else:
                    logger.info(f"Ignoring duplicate event for {label} on {camera} camera.")
                    logger.debug(f"Event Data: {event_data}")
            else:
                logger.info(f"Ignoring {label} on {camera} camera during cooldown period.")
                logger.debug(f"Event Data: {event_data}")
        else:
            logger.info(f"Ignored {label} on {camera} camera due to empty entered_zones.")
            logger.debug(f"Event Data: {event_data}")
                        
    # Handling the end event
    elif event_type == "end":
        if event_id in processed_events:
            logger.info(f"Sending end event for {label} on {camera} camera.")
            logger.debug(f"Event Data: {event_data}")
            
            # Optionally remove the event ID from the set of processed events
            processed_events.remove(event_id)


# Load config from YAML
config = load_config()

# Accessing specific settings from the configuration
mqtt_config = config['mqtt']
pushover_config = config['pushover']
frigate_server_config = config['frigate_server']
web_server_config = config['web_server']
cooldown_period = config['cooldown_period']
log_info = config['log_info']
healthchecks_config = config['healthchecks']

processed_events = set()
last_ping_time = None
cooldown_dict = {}  # Initialize the cooldown dictionary
frigate_server = frigate_server_config['host']
web_server = web_server_config['url']

# Setup Logging
logging_level = log_info['level']
log_file_path = log_info['log_file']
log_to_screen = log_info['log_to_screen']
valid_logging_levels = ['CRITICAL', 'ERROR', 'WARNING', 'INFO', 'DEBUG', 'NOTSET']

if logging_level not in valid_logging_levels:
    print(f"Invalid logging level specified: {logging_level}. Exiting.")
    exit(1)

# Check if Log File is Writable
try:
    with open(log_file_path, 'a') as f:
        pass
except IOError as e:
    print(f"Could not open log file {log_file_path} for writing. Exiting.")
    exit(1)

# Initialize Logging
logger = logging.getLogger(__name__)
logger.setLevel(logging_level)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# Setup Log File Handle and add to the logger
file_handler = logging.FileHandler(log_file_path)
file_handler.setLevel(logging_level)
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# Setup Screen logging
if log_to_screen:
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging_level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

atexit.register(exit_handler)
logger.info("Starting Frigate Notify.")

def main():
    # Setup flask
    app = Flask(__name__)

    # Setup the various routes to provide web services and proxy requests to the backend Frigate Server
    @app.route('/api/proxy/events/<event_id>/retain', methods=['DELETE'])
    def proxy_unretain_event(event_id):
        frigate_url = f'https://frigate.wrightfamily.org/api/events/{event_id}/retain'
        response = requests.delete(frigate_url)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to unretain event"}), response.status_code

    @app.route('/api/proxy/events/<event_id>', methods=['DELETE'])
    def proxy_delete_event(event_id):
        frigate_url = f'{frigate_server}/api/events/{event_id}'
        response = requests.delete(frigate_url)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to delete event"}), response.status_code

    @app.route('/api/proxy/events/<event_id>/retain', methods=['POST'])
    def proxy_retain_event(event_id):
        frigate_url = f'{frigate_server}/api/events/{event_id}/retain'
        response = requests.post(frigate_url)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to retain event"}), response.status_code

    @app.route('/event/<event_id>')
    def serve_event_page(event_id):
        return render_template('event.html', event_id=event_id)
    
    @app.route('/error')
    def error():
        return render_template('error.html')
    
    @app.route('/api/events/<event_id>/snapshot.jpg')
    def proxy_snapshot(event_id):
        response = requests.get(f'{frigate_server}/api/events/{event_id}/snapshot.jpg')
        return Response(response.content, mimetype='image/jpeg')

    @app.route('/api/events/<event_id>/clip.mp4')
    def proxy_clip(event_id):
        response = requests.get(f'{frigate_server}/api/events/{event_id}/clip.mp4')
        return Response(response.content, mimetype='video/mp4')
    
    @app.route('/api/proxy/events/<event_id>')
    def proxy_event_request(event_id):
        frigate_url = f'{frigate_server}/api/events/{event_id}'
        response = requests.get(frigate_url)
        
        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to fetch event information"}), response.status_code


    mqtt_thread = threading.Thread(target=connect_to_mqtt)
    mqtt_thread.start()

    # Start Flask app (this will block)
    app.run(host='0.0.0.0', port=5050)

if __name__ == '__main__':
    main()
