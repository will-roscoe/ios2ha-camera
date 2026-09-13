import socket
import threading
import time
from urllib.parse import urlparse

from tests.fakes.fake_camera import CREDENTIALS, FakeCamera

from ios2ha_camera.ingest import Ingestor
from ios2ha_camera.ring_buffer import RingBuffer

# Short timings so the suite stays fast; production defaults are seconds.
FAST = {"backoff_initial": 0.05, "backoff_max": 0.2, "read_timeout": 2.0, "healthy_after_s": 10.0}


def _ing(host, port, buf, states, password=CREDENTIALS[1], **kw):
    return Ingestor(host, port, CREDENTIALS[0], password, buf, states.append, **{**FAST, **kw})


def _cam_ing(cam, buf, states, **kw):
    u = urlparse(cam.url)
    return _ing(u.hostname, u.port, buf, states, **kw)


def _wait_for(pred, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


class _StallingServer:
    """Answers 200 with multipart headers and then never sends a byte: a frozen app."""

    def __enter__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.accepted = threading.Event()
        self._conns = []
        self._stop = threading.Event()

        def serve():
            while not self._stop.is_set():
                try:
                    c, _ = self._sock.accept()
                except OSError:
                    continue
                self._conns.append(c)
                c.recv(4096)
                c.sendall(b"HTTP/1.0 200 OK\r\n"
                          b"Content-Type: multipart/x-mixed-replace; boundary=guardianeyeframe\r\n\r\n")
                self.accepted.set()

        self._t = threading.Thread(target=serve, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()
        self._t.join(2)
        for c in self._conns:
            c.close()
        self._sock.close()


def _closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_fills_buffer_and_reports_streaming():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: buf.latest() is not None)
        ing.stop()
    assert states[0] == "streaming"
    assert buf.latest().data[:3] == b"\xff\xd8\xff"


def test_frames_carry_arrival_time():
    buf = RingBuffer(5_000_000); states = []
    before = time.time()
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: len(buf.recent(3)) == 3)
        ing.stop()
    tss = [f.ts for f in buf.recent(3)]
    assert tss == sorted(tss) and before <= tss[0] <= time.time()


def test_reports_streaming_off_on_503():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        cam.set_mode("off")
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: "streaming_off" in states)
        ing.stop()
    assert "streaming" not in states


def test_reports_unreachable_when_connection_refused():
    buf = RingBuffer(5_000_000); states = []
    ing = _ing("127.0.0.1", _closed_port(), buf, states); ing.start()
    assert _wait_for(lambda: "unreachable" in states)
    ing.stop()


def test_reports_unreachable_when_connection_drops():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        cam.set_mode("drop")
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: "unreachable" in states)
        ing.stop()


def test_wrong_password_is_not_reported_as_streaming():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states, password="wrong"); ing.start()
        assert _wait_for(lambda: states)
        ing.stop()
    assert states == ["unreachable"]
    assert buf.latest() is None


def test_does_not_claim_streaming_before_a_frame_arrives():
    """A frozen app can answer 200 and then send nothing. Reporting 'streaming'
    on the status line alone would hide exactly the failure this service exists
    to surface."""
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera(frames=0) as cam:
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: cam.requests >= 2)
        ing.stop()
    assert "streaming" not in states


def test_stream_that_keeps_ending_is_not_reconnected_in_a_tight_loop():
    """Rapid connect/close wedges the phone app until a manual restart. A
    stream that ends cleanly must still back off before reconnecting."""
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera(frames=1) as cam:
        ing = _cam_ing(cam, buf, states, backoff_initial=0.1, backoff_max=0.4); ing.start()
        time.sleep(1.5)
        ing.stop()
        requests = cam.requests
    # Sleeps of 0.1, 0.2, 0.4, 0.4, 0.4 fit about six attempts into 1.5 s;
    # reconnecting without a pause makes hundreds.
    assert 2 <= requests <= 8, requests


def test_recovers_when_streaming_is_switched_back_on():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        cam.set_mode("off")
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: "streaming_off" in states)
        cam.set_mode("stream")
        assert _wait_for(lambda: states[-1] == "streaming")
        ing.stop()
    assert states == ["streaming_off", "streaming"]


def test_stalled_stream_is_detected_as_unreachable():
    buf = RingBuffer(5_000_000); states = []
    with _StallingServer() as srv:
        ing = _ing("127.0.0.1", srv.port, buf, states, read_timeout=0.3); ing.start()
        assert _wait_for(lambda: "unreachable" in states, timeout=3.0)
        ing.stop()


def test_stop_returns_promptly_while_blocked_on_a_read():
    buf = RingBuffer(5_000_000); states = []
    with _StallingServer() as srv:
        ing = _ing("127.0.0.1", srv.port, buf, states, read_timeout=30.0); ing.start()
        assert srv.accepted.wait(3)
        time.sleep(0.1)  # let the reader block inside the read
        t0 = time.monotonic()
        ing.stop()
        elapsed = time.monotonic() - t0
    assert elapsed < 1.0, elapsed
    assert not ing._thread.is_alive()


def test_stop_reports_whether_the_thread_actually_exited():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: buf.latest() is not None)
        assert ing.stop() is True
    assert not ing._thread.is_alive()


def test_restarting_never_leaves_two_threads_connected_to_the_phone():
    """The phone must have exactly one client. A second ingest thread both
    doubles the load and churns connections, which is what wedges the app."""
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states)
        ing.start()
        first = ing._thread
        assert _wait_for(lambda: buf.latest() is not None)
        ing.start()                      # restart without an explicit stop
        second = ing._thread
        try:
            assert second is not first
            assert not first.is_alive(), "the previous ingest thread was left running"
            assert _wait_for(lambda: second.is_alive())
        finally:
            ing.stop()
    assert threading.active_count() < 20


def test_connect_to_a_blackholed_address_is_bounded_by_connect_timeout():
    """A phone asleep at the TCP level blocks in connect(), where there is no
    socket for stop() to shut down, so that wait must be short by itself."""
    buf = RingBuffer(5_000_000); states = []
    # 192.0.2.0/24 is TEST-NET-1: routable-looking, never answers.
    ing = _ing("192.0.2.1", 8080, buf, states, connect_timeout=0.3, read_timeout=30.0)
    ing.start()
    assert _wait_for(lambda: "unreachable" in states, timeout=5.0)
    t0 = time.monotonic()
    assert ing.stop() is True
    assert time.monotonic() - t0 < 3.0


def test_stopping_is_not_reported_as_a_failure():
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        ing = _cam_ing(cam, buf, states); ing.start()
        assert _wait_for(lambda: "streaming" in states)
        ing.stop()
    assert states == ["streaming"]


def test_each_session_reports_its_first_state_even_when_it_matches_the_last():
    """Upstream opens a session per hold and clears a failure between sessions
    when a probe succeeds, so a session failing exactly as the last one did
    must still say so, or the failure never reaches it."""
    buf = RingBuffer(5_000_000); states = []
    with FakeCamera() as cam:
        cam.set_mode("off")
        ing = _cam_ing(cam, buf, states)
        try:
            ing.start()
            assert _wait_for(lambda: states.count("streaming_off") == 1)
            ing.stop()
            ing.start()
            assert _wait_for(lambda: states.count("streaming_off") == 2), states
        finally:
            ing.stop()
