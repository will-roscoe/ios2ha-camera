"""The one persistent MJPEG connection to the phone, filling the ring buffer.

The phone app wedges under rapid connect/close, and a frozen app can answer 200
and then send nothing. So this client:

- reports `streaming` only once a frame has actually arrived;
- backs off before every reconnect, including after a stream that ended
  cleanly, and resets the backoff only after a connection that stayed healthy;
- times out a stalled read, which surfaces a frozen app as `unreachable`.
"""

import base64
import http.client
import logging
import socket
import threading
import time

from .mjpeg import iter_multipart
from .ring_buffer import Frame

log = logging.getLogger(__name__)

BOUNDARY = b"guardianeyeframe"


class _HttpStatus(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


class Ingestor:
    def __init__(self, host, port, user, password, buffer, on_state=lambda s: None, *,
                 backoff_initial=1.0, backoff_max=30.0, read_timeout=10.0,
                 healthy_after_s=10.0):
        self.host, self.port = host, port
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self.buffer = buffer
        self.on_state = on_state
        self.state = None
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.read_timeout = read_timeout
        self.healthy_after_s = healthy_after_s
        self._stop = threading.Event()
        self._thread = None
        self._sock = None
        self._sock_lock = threading.Lock()
        self._frames = 0

    def _set_state(self, s, reason=""):
        if s != self.state:
            log.info("stream state %s -> %s%s", self.state, s, f" ({reason})" if reason else "")
            self.state = s
            self.on_state(s)

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ingest", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # A read blocked on a silent socket would hold the thread for up to
        # read_timeout; shutting the socket down unblocks it at once.
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self):
        backoff = self.backoff_initial
        while not self._stop.is_set():
            started = time.monotonic()
            self._frames = 0
            try:
                self._stream_once()
                if not self._stop.is_set() and self._frames == 0:
                    self._set_state("unreachable", "stream ended without a frame")
            except Exception as e:  # refused, reset, timed out, bad status, malformed
                if self._stop.is_set():
                    break
                self._report_failure(e)
            if self._stop.is_set():
                break
            # Only a connection that stayed up earns a fast reconnect. One that
            # keeps ending quickly escalates instead of cycling at a fixed rate.
            if self._frames and time.monotonic() - started >= self.healthy_after_s:
                backoff = self.backoff_initial
            self._stop.wait(backoff)
            backoff = min(backoff * 2, self.backoff_max)

    def _stream_once(self):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.read_timeout)
        try:
            conn.connect()
            # Captured here because getresponse() drops conn.sock for a
            # close-delimited response, and stop() needs it to unblock a read.
            with self._sock_lock:
                self._sock = conn.sock
            if self._stop.is_set():
                return
            conn.request("GET", "/mjpeg", headers={"Authorization": self._auth})
            resp = conn.getresponse()
            if resp.status != 200:
                raise _HttpStatus(resp.status)
            for jpeg in iter_multipart(resp, BOUNDARY):
                if self._stop.is_set():
                    return
                self.buffer.append(Frame(jpeg, time.time()))
                self._frames += 1
                if self._frames == 1:
                    self._set_state("streaming")
        finally:
            with self._sock_lock:
                self._sock = None
            conn.close()

    def _report_failure(self, e):
        if isinstance(e, _HttpStatus) and e.status == 503:
            self._set_state("streaming_off", "the app answered 503: streaming is off")
        elif isinstance(e, _HttpStatus):
            self._set_state("unreachable", f"the app answered HTTP {e.status}")
        else:
            self._set_state("unreachable", f"{type(e).__name__}: {e}")
