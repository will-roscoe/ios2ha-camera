"""The timelapse builder: one build at a time, and every outcome said out loud.

Whatever asked -- a button, MQTT, the API, the page -- hears back the same way: the
`timelapse` state, and an event naming the build and why.
"""
import io
import shutil
import threading
import time

import pytest
from PIL import Image

from ios2ha_camera import timelapse
from ios2ha_camera.archive import Archive
from ios2ha_camera.timelapse import (
    MAX_WAITING,
    TIMELAPSE_STATES,
    EncodeError,
    TimelapseBuilder,
    parse_spec,
)

NOW = time.mktime((2026, 9, 28, 14, 0, 0, 0, 0, -1))


class Recorder:
    def __init__(self):
        self.state, self.events = {}, []

    def publish(self, d):
        self.state.update(d)

    def event(self, d):
        self.events.append(d)


def _archive(tmp_path, n=12):
    a = Archive(str(tmp_path / "archive"))
    for i in range(n):
        b = io.BytesIO()
        Image.new("RGB", (64, 48), (i * 20 % 255, 80, 160)).save(b, "JPEG")
        a.write_still(b.getvalue(), NOW - 3600 + i * 300)
    return a.root


def _builder(tmp_path, **kw):
    r = Recorder()
    b = TimelapseBuilder(str(tmp_path / "out"), _archive(tmp_path), r.publish, r.event, **kw)
    return b, r


def _fake_encode(stills, spec, out_dir, progress=lambda d, t: None, ffmpeg="ffmpeg"):
    progress(len(stills), len(stills))
    return {"name": spec.name, "kind": spec.kind, "range": spec.range, "start": spec.start,
            "end": spec.end, "first": spec.start, "last": spec.end, "frames": len(stills),
            "fps": spec.fps, "width": 64, "height": 48, "duration_s": 1.0, "bytes": 100,
            "built": NOW, "spec": {}}


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(timelapse, "encode", _fake_encode)


def test_the_states_the_sensor_can_take():
    assert TIMELAPSE_STATES[0] == "idle"
    assert {"queued", "building", "built", "rejected_duplicate", "rejected_busy",
            "failed_no_stills", "failed_encoder", "deleted", "rejected_unknown"} <= set(TIMELAPSE_STATES)


def test_a_request_is_queued_then_built(tmp_path, fake):
    b, r = _builder(tmp_path)
    assert b.state()["timelapse"] == "idle"
    assert b.request(parse_spec({"range": "12h"}, NOW)) == "queued"
    assert r.state["timelapse"] == "queued"
    assert b.run_once() == "built"
    s = b.state()
    assert s["timelapse"] == "built" and s["timelapse_progress"] == 100
    assert s["timelapse_count"] == 1
    entry = s["timelapses"]["last_12h"]
    assert entry["url"] == "/api/v1/timelapse/last_12h.mp4" and entry["frames"] == 12
    assert "spec" not in entry
    assert r.state["timelapses"] == s["timelapses"]
    assert r.events[-1]["result"] == "built" and r.events[-1]["name"] == "last_12h"


def test_the_same_name_twice_is_refused(tmp_path, fake):
    b, r = _builder(tmp_path)
    b.request(parse_spec({"range": "12h"}, NOW))
    assert b.request(parse_spec({"range": "12h"}, NOW)) == "rejected_duplicate"
    assert r.events[-1]["reason"]


def test_a_full_queue_is_refused(tmp_path, fake):
    b, _ = _builder(tmp_path)
    for i in range(MAX_WAITING):
        assert b.request(parse_spec({"start": "2026-09-28T13:00:00", "name": f"n{i}"}, NOW)) == "queued"
    assert b.request(parse_spec({"range": "7d"}, NOW)) == "rejected_busy"


def test_no_stills_in_the_span(tmp_path, fake):
    b, r = _builder(tmp_path)
    b.request(parse_spec({"start": "2026-01-01T00:00:00", "end": "2026-01-02T00:00:00"}, NOW))
    assert b.run_once() == "failed_no_stills"
    assert b.state()["timelapse_count"] == 0


def test_an_encoder_failure_leaves_the_builder_working(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise EncodeError("ffmpeg failed: Unknown encoder libx264")
    monkeypatch.setattr(timelapse, "encode", broken)
    b, r = _builder(tmp_path)
    b.request(parse_spec({"range": "12h"}, NOW))
    assert b.run_once() == "failed_encoder"
    assert "libx264" in r.events[-1]["reason"]
    monkeypatch.setattr(timelapse, "encode", _fake_encode)
    b.request(parse_spec({"range": "12h"}, NOW))
    assert b.run_once() == "built"


def test_too_many_stills_is_refused_when_it_is_found(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(timelapse, "MAX_FRAMES", 5)
    b, r = _builder(tmp_path)
    b.request(parse_spec({"range": "12h"}, NOW))
    assert b.run_once() == "rejected_invalid"
    assert "every" in r.events[-1]["reason"]


def test_delete(tmp_path, fake):
    b, _ = _builder(tmp_path)
    assert b.delete("nothing") == "rejected_unknown"
    assert b.delete("../etc") == "rejected_unknown"
    b.request(parse_spec({"range": "12h"}, NOW))
    b.run_once()
    assert b.delete("last_12h") == "deleted"
    assert b.state()["timelapses"] == {}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_the_worker_builds_for_real_and_the_catalogue_survives(tmp_path):
    b, r = _builder(tmp_path)
    b.start()
    try:
        b.request(parse_spec({"range": "12h", "fps": 6}, NOW))
        deadline = time.time() + 30
        while r.state.get("timelapse") != "built" and time.time() < deadline:
            time.sleep(0.05)
    finally:
        b.stop()
    assert r.state["timelapse"] == "built"
    again, _ = _builder(tmp_path)
    assert again.state()["timelapses"]["last_12h"]["frames"] == 12
    assert not any(t.name == "timelapse" for t in threading.enumerate())
