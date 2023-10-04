# Use the official Python image as the base image
FROM python:3-slim-buster

# Copy the default config.yaml into a dedicated config directory within the container
COPY config.yaml /config/config.yaml

# Copy the local script directory into the container
COPY ./frigatenotify.py /app/frigatenotify.py
COPY ./templates /app/templates

# Set the working directory
WORKDIR /app

# Install the necessary libraries
RUN pip install Flask paho-mqtt requests PyYAML

# Expose the port the app runs on
EXPOSE 5050

# Command to run the script
CMD ["python", "frigate_notify.py"]
