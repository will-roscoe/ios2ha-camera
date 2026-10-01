"""Timelapses built from the service's own archive, on request.

Until 2.16.0 a Home Assistant automation copied every still into /media/timelapse and
four scripts ran ffmpeg there. Will asked for it to move into the service, built on
request from a JSON spec "for later extensibility", and reachable the way everything
else is: the same request from MQTT, the API, the control page and the HA component.

A build request is a spec (`parse_spec`): a rolling range or a custom span, and how to
render it. The stills come from the archive, which is never pruned (`select_stills`);
ffmpeg encodes them (`encode`) at low priority so stacking keeps the host; and a
builder (`TimelapseBuilder`) runs one build at a time and keeps the catalogue.
"""

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from PIL import Image

log = logging.getLogger(__name__)

# Rolling ranges, in seconds. A month is thirty days.
RANGES = {"12h": 43_200, "24h": 86_400, "7d": 604_800, "1mo": 2_592_000}

SPEC_VERSION = 1
# key: (lowest, highest, default). `width` has no default: the stills' own size.
_INTS = {"fps": (1, 60, 15), "width": (160, 3840, None), "every": (1, 1000, 1),
         "quality": (15, 40, 23), "version": (SPEC_VERSION, SPEC_VERSION, SPEC_VERSION)}
_KEYS = {"range", "start", "end", "name", *_INTS}
NAME = re.compile(r"^[a-z0-9_-]{1,48}$")
# Range builds are named for their range and replace the last one; no custom build
# may take one of those names.
RESERVED = "last_"


class SpecError(ValueError):
    """A build request that cannot be built, with what to change."""


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str                # "range" | "custom"
    range: str | None
    start: float             # epoch seconds
    end: float
    fps: int
    width: int | None
    every: int
    quality: int


def _int(value, key):
    lo, hi, default = _INTS[key]
    if value is None:
        return default
    # bool is an int in Python; `true` in a JSON spec is a mistake, not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise SpecError(f"{key} must be a whole number from {lo} to {hi}, not {value!r}")
    if not lo <= value <= hi:
        raise SpecError(f"{key} must be from {lo} to {hi}, not {value}")
    return value


def _time(value, key):
    try:
        return datetime.fromisoformat(value).timestamp()   # naive: local time
    except (TypeError, ValueError) as e:
        raise SpecError(f"{key} must be an ISO 8601 time such as 2026-09-28T18:00, "
                        f"not {value!r}") from e


def _stamp(t):
    return time.strftime("%Y%m%d-%H%M", time.localtime(t))


def parse_spec(value, now):
    """A build request as a `Spec`, or SpecError saying which key is wrong."""
    if not isinstance(value, dict):
        raise SpecError(f"a timelapse spec is a JSON object such as {{\"range\": \"7d\"}}, "
                        f"not {value!r}")
    unknown = sorted(set(value) - _KEYS)
    if unknown:
        raise SpecError(f"unknown key {unknown[0]!r}; a spec takes {sorted(_KEYS)}")
    ints = {k: _int(value.get(k), k) for k in _INTS}
    if ints["width"] is not None and ints["width"] % 2:
        raise SpecError(f"width must be even, not {ints['width']}")
    has_range, has_start = "range" in value, "start" in value
    if has_range == has_start:
        raise SpecError("give either range (12h, 24h, 7d or 1mo) or start, not "
                        + ("both" if has_range else "neither"))
    name = value.get("name")
    if has_range:
        rng = value["range"]
        if rng not in RANGES:
            raise SpecError(f"range must be one of {list(RANGES)}, not {rng!r}")
        if name is not None or "end" in value:
            raise SpecError(f"a range build is always named {RESERVED}{rng} and ends now; "
                            "give start and end for a span of your own")
        start, end, kind, name = now - RANGES[rng], now, "range", f"{RESERVED}{rng}"
    else:
        rng, kind = None, "custom"
        start = _time(value["start"], "start")
        end = _time(value["end"], "end") if "end" in value else now
        if start >= end:
            raise SpecError("start must be before end")
        if name is None:
            name = f"custom_{_stamp(start)}_{_stamp(end)}"
        elif not isinstance(name, str) or not NAME.match(name):
            raise SpecError(f"name must be 1-48 of a-z, 0-9, _ and -, not {name!r}")
        elif name.startswith(RESERVED):
            raise SpecError(f"name may not start with {RESERVED!r}: those are the range builds")
    return Spec(name=name, kind=kind, range=rng, start=start, end=end, fps=ints["fps"],
                width=ints["width"], every=ints["every"], quality=ints["quality"])


# -- choosing the stills ------------------------------------------------------------

# A year of stills at one every ten minutes is about 52 000: more than this and the
# request is refused with the `every` that would fit, rather than encoding for hours.
MAX_FRAMES = 20_000
_STILL = re.compile(r"(\d{4})/(\d{2})/(\d{2})/(\d{6})\.jpg$")


def still_time(path):
    """When an archived still was taken, from its path; None for anything else.
    Local time, as `Archive.write_still` names them."""
    m = _STILL.search(path.replace("\\", "/"))
    if m is None:
        return None
    y, mo, d, hms = m.groups()
    try:
        return time.mktime(time.strptime(f"{y}{mo}{d}{hms}", "%Y%m%d%H%M%S"))
    except ValueError:
        return None


def select_stills(root, start, end, every):
    """The archive's stills from `start` to `end` inclusive, oldest first, every
    `every`th from the first. Only the day folders the span touches are read."""
    found = []
    day = time.mktime(time.localtime(start)[:3] + (0, 0, 0, 0, 0, -1))
    while day <= end:
        folder = os.path.join(root, time.strftime("%Y/%m/%d", time.localtime(day)))
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            names = []
        for n in names:
            t = still_time(os.path.join(folder, n))
            if t is not None and start <= t <= end:
                found.append((t, os.path.join(folder, n)))
        # The next local midnight, whatever the clocks did in between.
        nxt = time.localtime(day + 26 * 3600)
        day = time.mktime(nxt[:3] + (0, 0, 0, 0, 0, -1))
    picked = [p for _, p in sorted(found)][::every]
    if len(picked) > MAX_FRAMES:
        fits = -(-len(found) // MAX_FRAMES)
        raise SpecError(f"{len(picked)} stills is more than {MAX_FRAMES}; "
                        f"use every {fits} or a shorter span")
    return picked


# -- encoding -----------------------------------------------------------------------

class EncodeError(RuntimeError):
    """ffmpeg could not make the video; the message says why."""


def media_path(name):
    """Where a built timelapse is served: `entities.MEDIA`'s timelapse path."""
    return f"/api/v1/timelapse/{name}.mp4"


def _even(n):
    return max(2, int(n) // 2 * 2)


def _frame_size(spec, stills):
    """One size for the whole video, from the first readable still: a camera whose
    resolution changed mid-span would otherwise hand the encoder frames of two sizes."""
    for path in stills:
        try:
            with Image.open(path) as im:
                w0, h0 = im.size
        except OSError:
            continue
        w = _even(spec.width or w0)
        return w, _even(round(w * h0 / w0))
    raise EncodeError(f"none of the {len(stills)} stills is a readable image")


def _command(ffmpeg, spec, list_path, out, size):
    w, h = size
    scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p")
    cmd = [ffmpeg, "-nostdin", "-y", "-hide_banner", "-loglevel", "error",
           "-f", "concat", "-safe", "0", "-r", str(spec.fps), "-i", list_path,
           "-vf", scale, "-c:v", "libx264", "-preset", "veryfast", "-crf", str(spec.quality),
           # Two threads and the lowest priority: the host is a four-core box that
           # is also stacking every ten minutes.
           "-threads", "2", "-movflags", "+faststart", "-progress", "pipe:1", "-f", "mp4", out]
    nice = shutil.which("nice")
    return ([nice, "-n", "19"] if nice else []) + cmd


def encode(stills, spec, out_dir, progress=lambda done, total: None, ffmpeg="ffmpeg"):
    """Encode `stills` into `<out_dir>/<spec.name>.mp4` with a sidecar
    `<spec.name>.json`, both replaced only once complete. Returns the sidecar."""
    os.makedirs(out_dir, exist_ok=True)
    final = os.path.join(out_dir, f"{spec.name}.mp4")
    tmp, list_path = final + ".tmp", os.path.join(out_dir, f"{spec.name}.txt.tmp")
    total = len(stills)
    size = _frame_size(spec, stills)
    # ffmpeg leaves out a still it cannot decode and carries on, so the frames are
    # counted from what it reports having encoded, not from the list.
    encoded = 0
    with open(list_path, "w") as f:
        for p in stills:
            f.write("file '" + p.replace("'", "'\\''") + "'\n")
    try:
        with tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(_command(ffmpeg, spec, list_path, tmp, size),
                                        stdout=subprocess.PIPE, stderr=err, text=True)
            except OSError as e:
                raise EncodeError(f"ffmpeg could not be started ({ffmpeg}): {e}") from e
            for line in proc.stdout:
                if line.startswith("frame="):
                    with contextlib.suppress(ValueError):
                        encoded = int(line[6:])
                        progress(min(encoded, total), total)
            code = proc.wait()
            err.seek(0)
            message = err.read().decode(errors="replace").strip()
        if code != 0:
            tail = message.splitlines()[-1] if message else f"exit status {code}"
            raise EncodeError(f"ffmpeg failed: {tail}")
        os.replace(tmp, final)
    finally:
        for p in (tmp, list_path):
            with contextlib.suppress(FileNotFoundError):
                os.remove(p)
    progress(total, total)
    meta = {"name": spec.name, "kind": spec.kind, "range": spec.range,
            "start": spec.start, "end": spec.end,
            "first": still_time(stills[0]), "last": still_time(stills[-1]),
            "frames": encoded, "fps": spec.fps, "width": size[0], "height": size[1],
            "duration_s": round(encoded / spec.fps, 2), "bytes": os.path.getsize(final),
            "built": time.time(), "spec": asdict(spec)}
    side = os.path.join(out_dir, f"{spec.name}.json")
    with open(side + ".tmp", "w") as f:
        json.dump(meta, f)
    os.replace(side + ".tmp", side)
    return meta


# -- the catalogue ------------------------------------------------------------------

def load_catalogue(out_dir):
    """Every complete timelapse in `out_dir`, by name: a video with its sidecar.
    Leftovers of a build cut short are removed; a half pair is logged and left."""
    try:
        names = os.listdir(out_dir)
    except FileNotFoundError:
        return {}
    found = {}
    for n in sorted(names):
        path = os.path.join(out_dir, n)
        if n.endswith(".tmp"):
            with contextlib.suppress(OSError):
                os.remove(path)
            continue
        stem, ext = os.path.splitext(n)
        if ext == ".json":
            if not os.path.exists(os.path.join(out_dir, stem + ".mp4")):
                log.warning("timelapse: %s has no video; ignored", n)
                continue
            try:
                with open(path) as f:
                    found[stem] = json.load(f)
            except (OSError, ValueError) as e:
                log.warning("timelapse: %s cannot be read, ignored: %s", n, e)
        elif ext == ".mp4" and not os.path.exists(os.path.join(out_dir, stem + ".json")):
            log.warning("timelapse: %s has no sidecar; ignored", n)
    return found


def delete(out_dir, name):
    """Remove a timelapse's video and sidecar."""
    if not NAME.match(name):
        raise ValueError(f"not a timelapse name: {name!r}")
    for ext in (".mp4", ".json"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(os.path.join(out_dir, name + ext))
