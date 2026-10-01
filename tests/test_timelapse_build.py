"""Encoding a timelapse with ffmpeg, and the catalogue kept beside the videos.

Real ffmpeg on tiny stills: what is checked is a playable file and an honest sidecar.
"""
import io
import json
import os
import shutil
import subprocess
import time

import pytest
from PIL import Image

from ios2ha_camera.archive import Archive
from ios2ha_camera.timelapse import (
    EncodeError,
    delete,
    encode,
    load_catalogue,
    media_path,
    parse_spec,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
T0 = time.mktime((2026, 9, 28, 12, 0, 0, 0, 0, -1))


def _stills(tmp_path, n=12, sizes=None):
    a = Archive(str(tmp_path / "archive"))
    paths = []
    for i in range(n):
        w, h = (sizes or {}).get(i, (64, 48))
        b = io.BytesIO()
        Image.new("RGB", (w, h), (i * 20 % 255, 80, 160)).save(b, "JPEG")
        paths.append(a.write_still(b.getvalue(), T0 + i * 600))
    return paths


def _spec(**kw):
    return parse_spec({"start": "2026-09-28T12:00:00", "end": "2026-09-28T14:00:00",
                       "name": "test", **kw}, T0 + 86400)


def _probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
                          "-show_entries", "stream=codec_name,width,height,nb_read_frames",
                          "-of", "json", path], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"][0]


def test_a_timelapse_is_encoded_with_its_sidecar(tmp_path):
    stills = _stills(tmp_path, sizes={3: (80, 60), 4: (80, 60)})
    seen = []
    meta = encode(stills, _spec(fps=6), str(tmp_path / "out"),
                  progress=lambda done, total: seen.append((done, total)))
    video = tmp_path / "out" / "test.mp4"
    stream = _probe(str(video))
    assert stream["codec_name"] == "h264"
    assert int(stream["nb_read_frames"]) == 12
    assert stream["height"] % 2 == 0 and stream["width"] == 64
    assert meta["frames"] == 12 and meta["fps"] == 6
    assert meta["duration_s"] == pytest.approx(2.0, abs=0.2)
    assert meta["bytes"] == video.stat().st_size
    assert seen[-1] == (12, 12)
    assert json.loads((tmp_path / "out" / "test.json").read_text())["frames"] == 12


def test_a_width_scales_it(tmp_path):
    encode(_stills(tmp_path), _spec(width=160), str(tmp_path / "out"))
    assert _probe(str(tmp_path / "out" / "test.mp4"))["width"] == 160


def test_the_catalogue_is_the_complete_pairs(tmp_path):
    out = tmp_path / "out"
    encode(_stills(tmp_path), _spec(), str(out))
    (out / "half.mp4.tmp").write_bytes(b"x")
    (out / "orphan.mp4").write_bytes(b"x")
    (out / "lonely.json").write_text("{}")
    cat = load_catalogue(str(out))
    assert list(cat) == ["test"]
    assert not (out / "half.mp4.tmp").exists()
    assert cat["test"]["frames"] == 12


def test_delete_removes_the_pair(tmp_path):
    out = tmp_path / "out"
    encode(_stills(tmp_path), _spec(), str(out))
    delete(str(out), "test")
    assert load_catalogue(str(out)) == {}
    assert os.listdir(out) == []


def test_without_ffmpeg_it_says_so(tmp_path):
    with pytest.raises(EncodeError, match="ffmpeg"):
        encode(_stills(tmp_path), _spec(), str(tmp_path / "out"), ffmpeg="/nonexistent/ffmpeg")


@pytest.mark.parametrize("bad", [0, 5])
def test_a_corrupt_still_is_left_out_and_the_sidecar_says_so(tmp_path, bad):
    """One damaged file must not cost a month's timelapse, and the sidecar counts
    the frames the video really has."""
    stills = _stills(tmp_path)
    with open(stills[bad], "wb") as f:
        f.write(b"not a jpeg at all")
    meta = encode(stills, _spec(), str(tmp_path / "out"))
    assert meta["frames"] == 11
    assert int(_probe(str(tmp_path / "out" / "test.mp4"))["nb_read_frames"]) == 11


def test_no_readable_still_is_an_error(tmp_path):
    stills = _stills(tmp_path, n=3)
    for p in stills:
        with open(p, "wb") as f:
            f.write(b"x")
    with pytest.raises(EncodeError, match="readable"):
        encode(stills, _spec(), str(tmp_path / "out"))


def test_an_encoder_failure_carries_its_message(tmp_path):
    fake = tmp_path / "ffmpeg"
    fake.write_text("#!/bin/sh\necho 'Unknown encoder libx264' >&2\nexit 1\n")
    fake.chmod(0o755)
    with pytest.raises(EncodeError, match="Unknown encoder libx264"):
        encode(_stills(tmp_path), _spec(), str(tmp_path / "out"), ffmpeg=str(fake))
    assert not any(p.endswith((".mp4", ".tmp")) for p in os.listdir(tmp_path / "out"))


def test_the_media_path():
    assert media_path("last_7d") == "/api/v1/timelapse/last_7d.mp4"
