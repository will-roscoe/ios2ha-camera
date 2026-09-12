import base64
import urllib.request

import pytest

from ios2ha_camera.mjpeg import PartTooLarge, iter_multipart
from tests.fakes.fake_camera import BOUNDARY, CREDENTIALS, FakeCamera, _jpeg


def _open(cam, path="/mjpeg"):
    req = urllib.request.Request(cam.url + path)
    token = base64.b64encode(("%s:%s" % CREDENTIALS).encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    return urllib.request.urlopen(req, timeout=5)


class _Stream:
    """Feeds fixed bytes through the read(n) interface iter_multipart expects."""

    def __init__(self, data, chunk=7):
        self._data = data
        self._chunk = chunk
        self._pos = 0

    def read(self, n=None):
        n = self._chunk if n is None else min(n, self._chunk)
        out = self._data[self._pos:self._pos + n]
        self._pos += len(out)
        return out


def _part(payload):
    return (b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n"
            + b"Content-Length: %d\r\n\r\n" % len(payload) + payload + b"\r\n")


def test_parses_frames_from_multipart():
    with FakeCamera() as cam:
        frames = []
        for jpeg in iter_multipart(_open(cam), BOUNDARY):
            assert jpeg[:3] == b"\xff\xd8\xff"
            frames.append(jpeg)
            if len(frames) >= 5:
                break
        assert len(frames) == 5


def test_frames_are_whole_and_distinct():
    """A truncated frame still starts with SOI, so check the terminator too."""
    with FakeCamera() as cam:
        frames = []
        for jpeg in iter_multipart(_open(cam), BOUNDARY):
            frames.append(jpeg)
            if len(frames) >= 4:
                break
    assert all(f.endswith(b"\xff\xd9") for f in frames)
    assert len(set(frames)) == 4


def test_frame_with_embedded_thumbnail_is_not_split():
    """An EXIF thumbnail is a second SOI/EOI pair inside the payload. Scanning
    for markers instead of honouring Content-Length would cut the frame short."""
    payload = _jpeg(0, thumbnail=True)
    assert payload.count(b"\xff\xd8\xff") > 1, "fixture is not exercising the case"
    frames = list(iter_multipart(_Stream(_part(payload) * 2), BOUNDARY))
    assert frames == [payload, payload]


def test_falls_back_to_marker_scan_without_content_length():
    payload = _jpeg(3)
    part = (b"--" + BOUNDARY + b"\r\nContent-Type: image/jpeg\r\n\r\n" + payload + b"\r\n")
    assert list(iter_multipart(_Stream(part * 2), BOUNDARY)) == [payload, payload]


def test_stops_cleanly_at_end_of_stream():
    assert list(iter_multipart(_Stream(b""), BOUNDARY)) == []


def test_refuses_to_buffer_an_unbounded_part():
    """Garbage that never terminates must not grow the buffer without limit;
    the phone's kernel panic that started this project was memory exhaustion."""
    junk = _Stream(b"\xff\xd8\xff" + b"\x00" * (4 << 20), chunk=65536)
    with pytest.raises(PartTooLarge):
        list(iter_multipart(junk, BOUNDARY, max_part_bytes=1 << 20))


def test_media_endpoints_require_basic_auth():
    with FakeCamera() as cam:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(cam.url + "/mjpeg", timeout=5)
        assert e.value.code == 401


def test_streaming_off_answers_503():
    with FakeCamera() as cam:
        cam.set_mode("off")
        with pytest.raises(urllib.error.HTTPError) as e:
            _open(cam)
        assert e.value.code == 503
