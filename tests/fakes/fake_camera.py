"""A stand-in for Guardian Eye on the phone.

Mirrors the real app closely enough to test against: HTTP Basic on the media
endpoints, a `guardianeyeframe` multipart boundary with Content-Length on every
part sent with chunked transfer encoding, a 503 when streaming is switched off
in the app, and an abrupt connection drop. Nothing here touches the real device.
"""

import base64
import http.server
import io
import socket
import threading

import numpy as np
from PIL import Image

CREDENTIALS = ("root", "william2004")
BOUNDARY = b"guardianeyeframe"


def _jpeg(seed, thumbnail=False):
    rng = np.random.default_rng(seed)
    arr = (rng.random((48, 64, 3)) * 12).astype("uint8")  # dark, noisy
    buf = io.BytesIO()
    image = Image.fromarray(arr)
    if thumbnail:
        # An EXIF thumbnail is itself a complete JPEG, so the payload then
        # contains a second SOI/EOI pair. Frames like this are why the parser
        # must not simply scan for markers.
        exif = image.getexif()
        thumb = io.BytesIO()
        image.resize((16, 12)).save(thumb, "JPEG", quality=91)
        image.save(buf, "JPEG", quality=91, exif=exif.tobytes() + thumb.getvalue())
    else:
        image.save(buf, "JPEG", quality=91)
    return buf.getvalue()


class FakeCamera:
    """Modes: 'stream' serves frames, 'off' answers 503 as the app does when
    streaming is not running, 'drop' cuts the connection before any response.

    chunked=True (the default) matches the real app's transfer encoding."""

    def __init__(self, require_auth=True, frames=200, thumbnails=False, chunked=True):
        self.mode = "stream"
        self.require_auth = require_auth
        self.frames = frames
        self.thumbnails = thumbnails
        self.chunked = chunked
        self.requests = 0
        self._seed = 0

    def set_mode(self, mode):
        self.mode = mode

    def __enter__(self):
        cam = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if cam.chunked else "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _authorised(self):
                if not cam.require_auth:
                    return True
                expected = "Basic " + base64.b64encode(
                    ("%s:%s" % CREDENTIALS).encode()
                ).decode()
                return self.headers.get("Authorization") == expected

            def _empty(self, code, *headers):
                self.send_response(code)
                for name, value in headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", "0")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def _chunk(self, data):
                self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n")

            def do_GET(self):
                cam.requests += 1
                if not self._authorised():
                    return self._empty(401, ("WWW-Authenticate", 'Basic realm="camera"'))
                if cam.mode == "off":
                    return self._empty(503)
                if cam.mode == "drop":
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if self.path == "/snapshot.jpg":
                    b = _jpeg(cam._seed, cam.thumbnails)
                    cam._seed += 1
                    self.send_response(200)
                    self.send_header("Content-Type", "image/jpeg")
                    self.send_header("Content-Length", str(len(b)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(b)
                    self.close_connection = True
                    return
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "multipart/x-mixed-replace; boundary=" + BOUNDARY.decode(),
                )
                self.send_header("Connection", "close")
                if cam.chunked:
                    self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.close_connection = True
                write = self._chunk if cam.chunked else self.wfile.write
                try:
                    for _ in range(cam.frames):
                        if cam.mode != "stream":
                            break
                        b = _jpeg(cam._seed, cam.thumbnails)
                        cam._seed += 1
                        write(b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
                              + b"Content-Length: %d\r\n\r\n" % len(b) + b + b"\r\n")
                    if cam.chunked:
                        self.wfile.write(b"0\r\n\r\n")
                except OSError:  # client went away mid-stream
                    pass

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self._srv.daemon_threads = True
        self.url = "http://127.0.0.1:%d" % self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *a):
        self._srv.shutdown()
        self._srv.server_close()  # without this the listening socket leaks per test
        self._thread.join(timeout=5)
