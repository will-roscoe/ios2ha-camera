FROM python:3.12-slim

# opencv-python-headless needs libglib at import time; it deliberately does not
# need libGL, which is the whole point of the headless wheel. pymobiledevice3
# speaks the usbmux protocol in Python, so no libimobiledevice is needed either
# -- only the host's /var/run/usbmuxd socket, which compose bind-mounts.
# ffmpeg builds the timelapses (timelapse.py): OpenCV's bundled FFmpeg has no
# H.264 encoder, and H.264 is the codec every browser plays.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies are installed from pyproject alone, against a stub package, so
# the ~100-package layer is cached against pyproject.toml and a source change
# does not reinstall opencv and numpy every time.
COPY pyproject.toml ./
RUN mkdir -p src/ios2ha_camera \
    && touch src/ios2ha_camera/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf src

COPY src ./src
RUN pip install --no-cache-dir --no-deps --force-reinstall .

# Provenance, baked in at build time. The deployment is an rsync of a working
# tree, so a semantic version alone cannot say whether this is a clean release
# or a release plus uncommitted edits; git describe --dirty can. deploy.sh
# passes both. Last, so a code change does not invalidate the layers above.
ARG GIT_DESCRIBE=unknown
ARG BUILD_DATE=unknown
ENV GIT_DESCRIBE=$GIT_DESCRIBE
ENV BUILD_DATE=$BUILD_DATE

# The dated still archive. Bind-mounted to the host, under /home, because the
# host's root filesystem has almost no space left.
VOLUME ["/data"]

# Unbuffered so `docker logs` shows a stalled service's last words immediately
# rather than holding them in a pipe buffer.
ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "ios2ha_camera"]
