import atexit
import datetime
import json
import logging
import os
import re
import requests
import threading
import sched
import sqlite3
import time
import yaml

import paho.mqtt.client as mqtt
from flask import Flask, request, jsonify, render_template, Response, redirect, url_for
from requests.exceptions import HTTPError, Timeout, ConnectionError

def exit_handler():
    logger.info("Frigate Notify is exiting.")

def send_healthcheck_ping():
    global last_ping_time
    current_time = datetime.datetime.now()
    if last_ping_time is None or (current_time - last_ping_time) >= datetime.timedelta(hours=1):
        logger.info("Healthcheck Ping Sent")
        api_url = f"https://hc-ping.com/{healthchecks_config['uuid']}"
        try:
            response = requests.get(api_url)
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

    response = requests.post("https://api.pushover.net/1/messages.json", data=payload, files=files)
    return response.json()

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
    if not re.match (r'^[a-zA-Z0-9_-]+(\.[a-zA-Z0-9_-]+)*$', config.get('database')):
        errors.append("Log file should be a valid file name.")

    if errors:
        print("Configuration errors detected:")
        for error in errors:
            print(f"  - {error}")
        exit(1)
    else:
        return True
    
def load_config(config_file='config.yaml'):
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

def initialize_db(db_name):
    if not os.path.exists(db_name):
        conn = sqlite3.connect(db_name)
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
    conn = sqlite3.connect(silence_db)
    c = conn.cursor()
    now = datetime.datetime.now()
    if camera_id:
        c.execute('SELECT * FROM silence_settings WHERE camera_id = ? AND silence_until > ?', (camera_id, now))
    else:
        c.execute('SELECT * FROM silence_settings WHERE silence_until > ?', (now,))
    settings = c.fetchall()
    conn.close()
    return settings

def set_silence_settings(camera_id, silence_until):
    try:
        conn = sqlite3.connect(silence_db)
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
        print(f"Database error: {e}")
    finally:
        conn.close()

def clear_silence_settings(camera_id=None):
    conn = sqlite3.connect(silence_db)
    c = conn.cursor()
    if camera_id:
        c.execute('DELETE FROM silence_settings WHERE camera_id = ?', (camera_id,))
    else:
        c.execute('DELETE FROM silence_settings')
    conn.commit()
    conn.close()

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
silence_db = config['database']
cameras = config['cameras']

processed_events = set()
last_ping_time = None
cooldown_dict = {}  # Initialize the cooldown dictionary
frigate_server = frigate_server_config['host']
web_server = web_server_config['url']

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
        all_settings = get_silence_settings()
        camera_settings = {setting[0]: setting[1] for setting in all_settings}
        return render_template('event.html', event_id=event_id, camera_settings=camera_settings)
    
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
        duration = int(request.form['duration'])  # Get the duration in minutes from the form
        cameras = request.form.getlist('camera[]')  # Get the selected cameras from the form as a list

        # Calculate the silence_until datetime based on the current time and duration
        silence_until = datetime.datetime.now() + datetime.timedelta(minutes=duration)

        # If 'all' is selected or multiple cameras are selected, set the silence settings for all/selected cameras
        if 'all' in cameras:
            for cam in config['cameras']:
                set_silence_settings(cam, silence_until)
        else:
            for camera in cameras:
                set_silence_settings(camera, silence_until)

        return jsonify({"status": "success", "message": "Silence settings updated successfully"})

    @app.route('/clear_silence/<camera_id>')
    def clear_silence(camera_id):
        clear_silence_settings(camera_id)
        return jsonify({"status": "success", "message": f"Silence settings cleared for {camera_id}"})
    
    @app.route('/clear_all_silence')
    def clear_all_silence():
        clear_silence_settings()
        return jsonify({"status": "success", "message": f"Silence settings cleared for all cameras."})


    mqtt_thread = threading.Thread(target=connect_to_mqtt)
    mqtt_thread.start()

    # Start Flask app (this will block)
    app.run(host='0.0.0.0', port=5050)

if __name__ == '__main__':
    main()
