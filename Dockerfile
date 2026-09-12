FROM python:3.12-slim

# opencv-python-headless needs libglib at import time; it deliberately does not
# need libGL, which is the whole point of the headless wheel. pymobiledevice3
# speaks the usbmux protocol in Python, so no libimobiledevice is needed either
# -- only the host's /var/run/usbmuxd socket, which compose bind-mounts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# The dated still archive. Bind-mounted to the host, under /home, because the
# host's root filesystem has almost no space left.
VOLUME ["/data"]

# Unbuffered so `docker logs` shows a stalled service's last words immediately
# rather than holding them in a pipe buffer.
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "ios2ha_camera"]
