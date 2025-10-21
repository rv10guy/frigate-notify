# Use Python 3.11 slim image
FROM python:3.11-slim

LABEL org.opencontainers.image.title="Frigate Notify"
LABEL org.opencontainers.image.description="Smart notification system for Frigate NVR with silence management"
LABEL org.opencontainers.image.source="https://github.com/yourusername/frigate-notify"

# Create app user for security
RUN useradd --create-home --shell /bin/bash appuser

# Set the working directory
WORKDIR /app

# Copy application files
COPY --chown=appuser:appuser ./frigatenotify.py /app/frigatenotify.py
COPY --chown=appuser:appuser ./templates /app/templates

# Install dependencies
RUN pip install --no-cache-dir Flask paho-mqtt requests PyYAML

# Create config, data, and logs directories
RUN mkdir -p /config /data /app/logs && \
    chown -R appuser:appuser /config /data /app/logs

# Switch to non-root user
USER appuser

# Expose the port the app runs on
EXPOSE 5050

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import requests; requests.get('http://localhost:5050/silence_settings', timeout=5)" || exit 1

# Environment variables (can be overridden at runtime)
ENV PYTHONUNBUFFERED=1

# Volumes for config, database, and logs
VOLUME ["/config", "/data", "/app/logs"]

# Command to run the script
CMD ["python", "-u", "frigatenotify.py"]
