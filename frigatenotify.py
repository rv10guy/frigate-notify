import atexit
import datetime
import json
import logging
import os
import random
import re
import requests
import threading
import sched
import sqlite3
import time
import yaml
from enum import Enum

import paho.mqtt.client as mqtt
from flask import Flask, request, jsonify, render_template, Response, redirect, url_for
from requests.exceptions import HTTPError, Timeout, ConnectionError

# MQTT Connection States
class MQTTConnectionState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    FAILED = "failed"

def exit_handler():
    logger.info("Frigate Notify is exiting.")

def cleanup_old_processed_events():
    """Periodically clean up old entries from processed_events to prevent memory leak"""
    while True:
        try:
            time.sleep(3600)  # Run cleanup every hour
            current_time = datetime.datetime.now()
            max_age = datetime.timedelta(hours=48)  # Remove events older than 48 hours

            with processed_events_lock:
                # Find events older than max_age
                old_events = [
                    event_id for event_id, timestamp in processed_events.items()
                    if (current_time - timestamp) > max_age
                ]

                # Remove old events
                for event_id in old_events:
                    del processed_events[event_id]

                if old_events:
                    logger.info(f"Cleaned up {len(old_events)} old processed events from memory")

        except Exception as e:
            logger.error(f"Error in cleanup_old_processed_events: {e}")

def send_healthcheck_ping():
    global last_ping_time
    current_time = datetime.datetime.now()
    if last_ping_time is None or (current_time - last_ping_time) >= datetime.timedelta(hours=1):
        logger.info("Healthcheck Ping Sent")
        api_url = f"https://hc-ping.com/{healthchecks_config['uuid']}"
        try:
            response = requests.get(api_url, timeout=10)
            response.raise_for_status()  # This will check for HTTP errors
            last_ping_time = current_time  # Update the last ping time
        except requests.RequestException as e:
            logger.error(f'An error occurred: {e} sending healthcheck ping')

def send_pushover_notification(
    token, user, message,
    ttl=None, attachment=None, html=None, sound=None,
    timestamp=None, title=None, url=None, url_title=None, **kwargs):

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

    # Retry logic with exponential backoff
    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post("https://api.pushover.net/1/messages.json", data=payload, files=files, timeout=15)
            response.raise_for_status()  # Raise exception for HTTP errors
            return response.json()
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff with jitter
                logger.warning(f"Pushover notification failed (attempt {attempt + 1}/{max_retries}): {e}. Retrying in {wait_time:.1f}s...")
                time.sleep(wait_time)
            else:
                logger.error(f"Pushover notification failed after {max_retries} attempts: {e}. Message: {message}")
                return {"status": 0, "error": str(e)}

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
    if not isinstance(healthchecks.get('uuid'), str):
        errors.append("Healthchecks uuid should be a string.")

    # Validate Frigate Server section
    frigate_server = config.get('frigate_server', {})
    if not re.match(r'https?://[^\s]+', frigate_server.get('host', '')):
        errors.append("Frigate Server host should be a valid URL.")

    # Validate Web Server section
    web_server = config.get('web_server', {})
    if not re.match(r'https?://[^\s]+', web_server.get('url', '')):
        errors.append("Web Server URL should be a valid URL.")

    # Validate Logging section
    log_info = config.get('log_info', {})
    if log_info.get('level') not in ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL']:
        errors.append("Logging level should be a valid Python logging level.")
    if log_info.get('log_file') and not re.match(r'^[\w\-/.]+$', log_info.get('log_file')):
        errors.append("Log file should be a valid file path.")
    if not isinstance(log_info.get('log_to_screen'), bool):
        errors.append("'log_to_screen' should be a boolean value (True/False).")

    #Validate other fields
    if not isinstance(config.get('cooldown_period'), int):
        errors.append("Cooldown period should be an integer.")
    if config.get('database') and not re.match(r'^[\w\-/.]+$', config.get('database')):
        errors.append("Database file should be a valid file path.")

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
            config = yaml.safe_load(f)

        # Override with environment variables if present (for secrets)
        # This allows secrets to be passed via environment variables instead of config file
        if 'pushover' not in config:
            config['pushover'] = {}
        config['pushover']['api_key'] = os.getenv('PUSHOVER_API_KEY', config.get('pushover', {}).get('api_key'))
        config['pushover']['user_key'] = os.getenv('PUSHOVER_USER_KEY', config.get('pushover', {}).get('user_key'))

        if 'mqtt' not in config:
            config['mqtt'] = {}
        config['mqtt']['username'] = os.getenv('MQTT_USERNAME', config.get('mqtt', {}).get('username'))
        config['mqtt']['password'] = os.getenv('MQTT_PASSWORD', config.get('mqtt', {}).get('password'))

        if 'healthchecks' not in config:
            config['healthchecks'] = {}
        config['healthchecks']['uuid'] = os.getenv('HEALTHCHECKS_UUID', config.get('healthchecks', {}).get('uuid'))

        if 'frigate_server' not in config:
            config['frigate_server'] = {}
        frigate_host = os.getenv('FRIGATE_SERVER_HOST', config.get('frigate_server', {}).get('host'))
        if frigate_host:
            config['frigate_server']['host'] = frigate_host

        # Validate configuration before using it
        if not validate_config(config):
            print("Configuration validation failed. Exiting.")
            exit(1)

        return config

    except FileNotFoundError as e:
        print(f"Configuration file not found: {e}")
        exit(1)
    except yaml.YAMLError as e:
        print(f"Error in configuration file: {e}")
        exit(1)

def initialize_db(db_name):
    if not os.path.exists(db_name):
        conn = sqlite3.connect(db_name, check_same_thread=False)
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS silence_settings (
                camera_id TEXT PRIMARY KEY,
                silence_until DATETIME
            )
        ''')
        conn.commit()
        conn.close()

def get_silence_settings(camera_id=None):
    try:
        conn = sqlite3.connect(silence_db, check_same_thread=False)
        c = conn.cursor()
        now = datetime.datetime.now()
        if camera_id:
            c.execute('SELECT * FROM silence_settings WHERE camera_id = ? AND silence_until > ?', (camera_id, now))
        else:
            c.execute('SELECT * FROM silence_settings WHERE silence_until > ?', (now,))
        settings = c.fetchall()
        return settings
    except Exception as e:
        logger.error(f"Database error in get_silence_settings: {e}")
        return []
    finally:
        conn.close()

def set_silence_settings(camera_id, silence_until):
    conn = None
    try:
        conn = sqlite3.connect(silence_db, check_same_thread=False)
        c = conn.cursor()
        query = '''
            INSERT OR REPLACE INTO silence_settings
            (camera_id, silence_until)
            VALUES (?, ?)
        '''
        params = (camera_id, silence_until)
        c.execute(query, params)
        conn.commit()
    except Exception as e:
        logger.error(f"Database error in set_silence_settings: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def clear_silence_settings(camera_id=None):
    conn = None
    try:
        conn = sqlite3.connect(silence_db, check_same_thread=False)
        c = conn.cursor()
        if camera_id:
            c.execute('DELETE FROM silence_settings WHERE camera_id = ?', (camera_id,))
        else:
            c.execute('DELETE FROM silence_settings')
        conn.commit()
    except Exception as e:
        logger.error(f"Database error in clear_silence_settings: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            conn.close()

def validate_camera_id(camera_id):
    """Validate that camera_id is in the configured cameras list"""
    return camera_id in cameras

def validate_event_id(event_id):
    """Validate event ID format (should be like: timestamp-hash or similar)"""
    # Frigate event IDs are typically in format like: 1698598234.123456-abcd1234
    # Allow alphanumeric, dots, dashes, underscores
    return bool(re.match(r'^[\w\.\-]+$', event_id)) and len(event_id) < 100

def validate_duration(duration_str):
    """Validate and convert duration to positive integer"""
    try:
        duration = int(duration_str)
        return duration > 0 and duration < 525600  # Max 1 year in minutes
    except (ValueError, TypeError):
        return False

def connect_to_mqtt():
    global mqtt_connection_state
    backoff_time = 1  # in seconds
    max_backoff_time = 60  # in seconds
    retry_count = 0

    while True:
        try:
            with mqtt_state_lock:
                mqtt_connection_state = MQTTConnectionState.CONNECTING

            logger.info(f"Attempting to connect to MQTT broker at {mqtt_config['host']}:{mqtt_config['port']} (attempt #{retry_count + 1})")

            # Use new callback API version 2 with stable client ID
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id="frigate-notify",  # Stable client ID
                clean_session=True,          # Don't persist session state
                protocol=mqtt.MQTTv311       # Use MQTT 3.1.1 protocol
            )
            client.on_connect = on_connect
            client.on_message = on_message
            client.on_disconnect = on_disconnect

            # Enable Paho logging
            client.enable_logger(logger)

            client.username_pw_set(mqtt_config['username'], mqtt_config['password'])

            # Increased keepalive to 120 seconds for better stability
            client.connect(mqtt_config['host'], mqtt_config['port'], keepalive=120)

            # Paho's built-in reconnect handling
            client.reconnect_delay_set(min_delay=1, max_delay=120)

            # Reset backoff on successful connection
            backoff_time = 1
            retry_count = 0

            logger.info(f"MQTT connection established with client_id: frigate-notify")

            # This blocks until disconnect
            client.loop_forever()

        except (mqtt.ClientException, OSError) as e:
            logger.error(f"MQTT Client Exception: {e}")
            with mqtt_state_lock:
                mqtt_connection_state = MQTTConnectionState.RECONNECTING
        except ConnectionRefusedError as e:
            logger.error(f"MQTT Connection Refused: {e}")
            with mqtt_state_lock:
                mqtt_connection_state = MQTTConnectionState.RECONNECTING
        except TimeoutError as e:
            logger.error(f"MQTT Connection Timed Out: {e}")
            with mqtt_state_lock:
                mqtt_connection_state = MQTTConnectionState.RECONNECTING
        except Exception as e:
            logger.exception(f"Unexpected MQTT error: {e}")
            with mqtt_state_lock:
                mqtt_connection_state = MQTTConnectionState.FAILED

        # Exponential backoff with jitter
        retry_count += 1
        jitter = random.uniform(0, 0.3 * backoff_time)  # Add up to 30% jitter
        sleep_time = min(backoff_time + jitter, max_backoff_time)
        logger.info(f"Reconnecting to MQTT in {sleep_time:.1f} seconds...")
        time.sleep(sleep_time)
        backoff_time = min(backoff_time * 2, max_backoff_time)

def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    """Callback for MQTT disconnection (API v2)"""
    global mqtt_connection_state
    with mqtt_state_lock:
        if reason_code != 0:
            mqtt_connection_state = MQTTConnectionState.RECONNECTING
            logger.warning(f"Unexpected MQTT disconnection. Will auto-reconnect. Reason code: {reason_code}")
        else:
            mqtt_connection_state = MQTTConnectionState.DISCONNECTED
            logger.info("MQTT client disconnected gracefully.")

def on_connect(client, userdata, flags, reason_code, properties):
    """Callback for MQTT connection (API v2)"""
    global mqtt_connection_state
    with mqtt_state_lock:
        if reason_code == 0:
            mqtt_connection_state = MQTTConnectionState.CONNECTED
            logger.info(f"Successfully connected to MQTT broker. Reason code: {reason_code}")
        else:
            mqtt_connection_state = MQTTConnectionState.FAILED
            logger.error(f"Failed to connect to MQTT broker. Reason code: {reason_code}")
            return

    # Subscribe to topics (only if connection successful)
    client.subscribe(mqtt_config['topic'])
    logger.info(f"Subscribed to main topic: {mqtt_config['topic']}")
    for door in doors:
        topic = door['topic']
        client.subscribe(topic)
        logger.info(f"Subscribed to door topic: {topic} for {door['door']}")


def on_message(client, userdata, msg):
    topic = msg.topic
    

    # Check if the topic is the one specified in mqtt_config
    if topic == mqtt_config['topic']:
        process_camera_event(msg)

    # Check if the topic exists in the doors list
    elif any(door['topic'] == topic for door in doors):
        payload = msg.payload.decode()
        process_door_event(payload, topic)

    else:
        logger.warning(f"Received message from unhandled topic: {topic}")

def process_door_event(payload, topic):

    if payload != "ON":
        return  # Exit the function early if the payload is not "ON"  

    # Look up the camera and door values based on the topic
    door_entry = next((door for door in doors if door['topic'] == topic), None)
    if door_entry is None:
        logger.warning(f"No door entry found for topic: {topic}")
        return
    
    camera = door_entry['camera']
    door_name = door_entry['door']

    # Get the silence settings for the desired camera
    silence_settings = get_silence_settings(camera)

    # If the silence settings were found for the desired camera
    if silence_settings:
        # Assume silence_settings is a list of tuples and get the first tuple
        silence_until_tuple = silence_settings[0]

        # Assume the silence_until value is the second element in the tuple
        silence_until_str = silence_until_tuple[1]

        # Convert silence_until to a datetime object
        silence_until = datetime.datetime.strptime(silence_until_str, '%Y-%m-%d %H:%M:%S.%f')

        current_time = datetime.datetime.now()
        remaining_silence_time = silence_until - current_time
        silence_period = datetime.timedelta(minutes=config['door_settings']['silence_period'])

        if remaining_silence_time < silence_period:
            # If the remaining silence time is less than config['door_settings']['silence_period'],
            # reset the silence time to have at least that much time
            new_silence_until = current_time + silence_period
            set_silence_settings(camera, new_silence_until)
            logger.info(f"{camera} was already silenced, extending time until {new_silence_until} because {door_name} was opened.")
        elif remaining_silence_time >= silence_period:
            # If the remaining silence time is longer than config['door_settings']['silence_period'],
            # cancel the update
            logger.info(f"{camera} has more than the silence period remaining. Ignoring the {door_name} opening trigger.")
            return

    # Check if there's a recent detection for this camera
    with detection_lock:
        last_detection_time = detection_dict.get(camera)
    no_detection_timeout = config['door_settings']['no_detection_timeout'] * 60
    if last_detection_time and (datetime.datetime.now() - last_detection_time).total_seconds() < no_detection_timeout:
        logger.info(f"No action taken, {camera} detected activity in the last {config['door_settings']['no_detection_timeout']} minutes.")
        return

    # Otherwise, silence the camera and update the detection_dict
    silence_until = datetime.datetime.now() + datetime.timedelta(minutes=config['door_settings']['silence_period'])
    set_silence_settings(camera, silence_until)
    with detection_lock:
        detection_dict[camera] = datetime.datetime.now()

    logger.info(f"{camera} is being silenced until {silence_until} minutes because {door_name} was opened.")



def process_camera_event(msg):
    payload = json.loads(msg.payload)
    event_type = payload["type"]
    event_data = payload["after"]
    event_id = event_data["id"]
    label = payload['after']['label'].capitalize()
    camera = payload['after']['camera'].capitalize()
    timestamp = datetime.datetime.now().strftime("%m/%d/%Y %I:%M:%S %p")
    
    current_time = datetime.datetime.now()
    result = send_healthcheck_ping()

    # Check silence settings for the camera
    silence_settings = get_silence_settings(camera)
    if silence_settings:
        logger.info(f"Ignoring {label} on {camera} camera due to silence setting.")
        return  # Exit the function early if the camera is silenced

    if event_type in ["new", "update"]:
        entered_zones = event_data.get("entered_zones", [])

        # If entered_zones is not empty, process the event
        if entered_zones:
            camera_label_combo = f"{event_data['camera']}_{event_data['label']}"
            with cooldown_lock:
                last_alert_time = cooldown_dict.get(camera_label_combo, None)

            if (not last_alert_time) or (current_time - last_alert_time).total_seconds() >= cooldown_period:
                with cooldown_lock:
                    cooldown_dict[camera_label_combo] = current_time  # Update the last alert time
                with detection_lock:
                    detection_dict[camera] = current_time

                with processed_events_lock:
                    event_already_processed = event_id in processed_events
                    if not event_already_processed:
                        processed_events[event_id] = current_time  # Store with timestamp

                if not event_already_processed:
           
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
        with processed_events_lock:
            if event_id in processed_events:
                logger.info(f"Sending end event for {label} on {camera} camera.")
                logger.debug(f"Event Data: {event_data}")

                # Remove the event ID from processed events
                del processed_events[event_id]


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
silence_db = config['database']
cameras = config['cameras']
doors = config['door_settings']['doors']

processed_events = {}  # Changed to dict with timestamps: {event_id: timestamp}
last_ping_time = None
cooldown_dict = {}  # Initialize the cooldown dictionary
detection_dict = {} # Intiialize the detection dictionary
frigate_server = frigate_server_config['host']
web_server = web_server_config['url']
mqtt_connection_state = MQTTConnectionState.DISCONNECTED  # Track MQTT connection state

# Thread safety locks for shared data structures
mqtt_state_lock = threading.Lock()  # Lock for MQTT state changes
processed_events_lock = threading.Lock()  # Lock for processed_events dict
cooldown_lock = threading.Lock()  # Lock for cooldown_dict
detection_lock = threading.Lock()  # Lock for detection_dict

# Initialize Database
initialize_db(silence_db)

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
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        frigate_url = f'{frigate_server}/api/events/{event_id}/retain'
        response = requests.delete(frigate_url, timeout=10)
        logger.info(f"Event {event_id} unretained from {request.remote_addr}")

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to unretain event"}), response.status_code

    @app.route('/api/proxy/events/<event_id>', methods=['DELETE'])
    def proxy_delete_event(event_id):
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        frigate_url = f'{frigate_server}/api/events/{event_id}'
        response = requests.delete(frigate_url, timeout=10)
        logger.info(f"Event {event_id} deleted from {request.remote_addr}")

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to delete event"}), response.status_code

    @app.route('/api/proxy/events/<event_id>/retain', methods=['POST'])
    def proxy_retain_event(event_id):
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        frigate_url = f'{frigate_server}/api/events/{event_id}/retain'
        response = requests.post(frigate_url, timeout=10)
        logger.info(f"Event {event_id} retained from {request.remote_addr}")

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to retain event"}), response.status_code

    @app.route('/event/<event_id>')
    def serve_event_page(event_id):
        if not validate_event_id(event_id):
            return render_template('error.html'), 400

        all_settings = get_silence_settings()
        camera_settings = {setting[0]: setting[1] for setting in all_settings}
        return render_template('event.html', event_id=event_id, camera_settings=camera_settings)
    
    @app.route('/error')
    def error():
        return render_template('error.html')
    
    @app.route('/api/events/<event_id>/snapshot.jpg')
    def proxy_snapshot(event_id):
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        response = requests.get(f'{frigate_server}/api/events/{event_id}/snapshot.jpg', timeout=15)
        return Response(response.content, mimetype='image/jpeg')

    @app.route('/api/events/<event_id>/clip.mp4')
    def proxy_clip(event_id):
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        response = requests.get(f'{frigate_server}/api/events/{event_id}/clip.mp4', timeout=30)
        return Response(response.content, mimetype='video/mp4')

    @app.route('/api/proxy/events/<event_id>')
    def proxy_event_request(event_id):
        if not validate_event_id(event_id):
            return jsonify({"error": "Invalid event ID"}), 400

        frigate_url = f'{frigate_server}/api/events/{event_id}'
        response = requests.get(frigate_url, timeout=10)

        if response.status_code == 200:
            return jsonify(response.json())
        else:
            return jsonify({"error": "Failed to fetch event information"}), response.status_code
    
    @app.route('/silence_settings')
    def silence_settings():
        all_settings = get_silence_settings()
        camera_settings = {setting[0]: setting[1] for setting in all_settings}
        return render_template('silence_settings.html', camera_settings=camera_settings, cameras=config['cameras'])

    @app.route('/get_camera_silence_settings', methods=['GET'])
    def get_camera_silence_settings_route():
        all_settings = get_silence_settings()
        camera_settings = {setting[0]: setting[1] for setting in all_settings}
        return jsonify(camera_settings)

    @app.route('/set_silence', methods=['POST'])
    def set_silence():
        # Validate duration
        duration_str = request.form.get('duration')
        if not duration_str or not validate_duration(duration_str):
            return jsonify({"status": "error", "message": "Invalid duration value"}), 400

        duration = int(duration_str)
        selected_cameras = request.form.getlist('camera[]')  # Get the selected cameras from the form as a list

        # Calculate the silence_until datetime based on the current time and duration
        silence_until = datetime.datetime.now() + datetime.timedelta(minutes=duration)

        # If 'all' is selected or multiple cameras are selected, set the silence settings for all/selected cameras
        if 'all' in selected_cameras:
            for cam in config['cameras']:
                set_silence_settings(cam, silence_until)
            logger.info(f"Silence set for all cameras until {silence_until} (duration: {duration} minutes) from {request.remote_addr}")
        else:
            # Validate camera IDs
            for camera in selected_cameras:
                if not validate_camera_id(camera):
                    return jsonify({"status": "error", "message": f"Invalid camera ID: {camera}"}), 400

            for camera in selected_cameras:
                set_silence_settings(camera, silence_until)
            logger.info(f"Silence set for cameras {selected_cameras} until {silence_until} (duration: {duration} minutes) from {request.remote_addr}")

        return jsonify({"status": "success", "message": "Silence settings updated successfully"})

    @app.route('/clear_silence/<camera_id>')
    def clear_silence(camera_id):
        if not validate_camera_id(camera_id):
            return jsonify({"status": "error", "message": f"Invalid camera ID: {camera_id}"}), 400

        clear_silence_settings(camera_id)
        logger.info(f"Silence cleared for camera {camera_id} from {request.remote_addr}")
        return jsonify({"status": "success", "message": f"Silence settings cleared for {camera_id}"})

    @app.route('/clear_all_silence')
    def clear_all_silence():
        clear_silence_settings()
        logger.info(f"Silence cleared for all cameras from {request.remote_addr}")
        return jsonify({"status": "success", "message": f"Silence settings cleared for all cameras."})


    # Start MQTT connection thread
    mqtt_thread = threading.Thread(target=connect_to_mqtt, daemon=True)
    mqtt_thread.start()

    # Start cleanup thread for processed_events
    cleanup_thread = threading.Thread(target=cleanup_old_processed_events, daemon=True)
    cleanup_thread.start()

    # Start Flask app (this will block)
    app.run(host='0.0.0.0', port=5050)

if __name__ == '__main__':
    main()
