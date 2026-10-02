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

import bisect
import collections
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime

from PIL import Image

from .sun import sun_event

log = logging.getLogger(__name__)

# Rolling ranges, in seconds. A month is thirty days.
RANGES = {"12h": 43_200, "24h": 86_400, "7d": 604_800, "1mo": 2_592_000}

SPEC_VERSION = 1
# key: (lowest, highest, default). No default for `width` (the stills' own size), nor
# for `fps` and `every`, which `fit` chooses to make about ten seconds of video.
_INTS = {"fps": (1, 60, None), "width": (160, 3840, None), "every": (1, 1000, None),
         "quality": (15, 40, 23), "version": (SPEC_VERSION, SPEC_VERSION, SPEC_VERSION)}
_PICKS = ("at", "interval", "sun")
_KEYS = {"range", "duration", "start", "end", "name", "sun_offset", "tolerance", *_PICKS,
         *_INTS}
_SPAN = re.compile(r"\A([+-]?)(\d+)(mo|m|h|d|w)\Z")
_UNITS = {"m": 60, "h": 3_600, "d": 86_400, "w": 604_800, "mo": 2_592_000}
_CLOCK = re.compile(r"\A([01]\d|2[0-3]):([0-5]\d)\Z")
SUN_EVENTS = ("sunrise", "sunset", "noon")
# How far from a picked time a still may be: half an interval, at most a quarter hour.
TOLERANCE = 900
# \A and \Z, not ^ and $: `$` also matches before a trailing newline, and a file
# named with one can never be fetched.
NAME = re.compile(r"\A[a-z0-9_-]{1,48}\Z")
# Range builds are named for their range and replace the last one; no custom build
# may take one of those names.
RESERVED = "last_"


class SpecError(ValueError):
    """A build request that cannot be built, with what to change."""


def _int_field(key, description):
    lo, hi, default = _INTS[key]
    field = {"type": "int", "min": lo, "max": hi, "description": description}
    if default is not None:
        field["default"] = default
    return field


# What a client needs to build a form for each action: served in /objects with the
# action, so the page, the HA component and anyone on MQTT read the same rules the
# parser enforces.
SPEC_FIELDS = {
    "range": {"type": "select", "options": ["12h", "24h", "7d", "1mo"],
              "description": "A rolling range ending now, rebuilt as last_<range>. "
                             "Give this, duration or start."},
    "duration": {"type": "str",
                 "description": "Any span ending now (or at end): 50h, 90m, 3d, 2w, 1mo (30 "
                                "days). Rebuilt as last_<duration> unless named."},
    "start": {"type": "datetime",
              "description": "Start of a span of your own, ISO 8601; without a zone it is "
                             "the service's local time."},
    "end": {"type": "datetime", "description": "End of that span; now if left out."},
    "name": {"type": "str",
             "description": "Name for a span of your own: up to 48 of a-z, 0-9, _ and -."},
    "at": {"type": "str",
           "description": "Only the still nearest these times of day, e.g. 06:00 or "
                          "06:00, 18:00."},
    "interval": {"type": "str",
                 "description": "Only the still nearest each step from the start, e.g. 24h "
                                "or 1h."},
    "sun": {"type": "select", "options": list(SUN_EVENTS),
            "description": "Only the still nearest each day's sunrise, sunset or solar "
                           "noon; needs the camera's latitude and longitude."},
    "sun_offset": {"type": "str", "description": "Shift the sun's time, e.g. +30m or -1h."},
    "tolerance": {"type": "str",
                  "description": "How far from a picked time a still may be; 15m (or half "
                               "the interval) if left out."},
    "fps": _int_field("fps", "Frames of video per second; chosen for about ten seconds "
                             "if left out."),
    "width": _int_field("width", "Width in pixels, even; the stills' own if left out."),
    "every": _int_field("every", "Use every Nth still; chosen for about ten seconds if "
                                 "left out."),
    "quality": _int_field("quality", "x264 CRF: lower is sharper and bigger."),
}
DELETE_FIELDS = {"name": {"type": "str", "description": "The timelapse to delete."}}


@dataclass(frozen=True)
class Spec:
    name: str
    kind: str                # "range" (ends now, rebuilt under its name) | "custom"
    range: str | None
    start: float             # epoch seconds
    end: float
    fps: float | None        # None: chosen by `fit`
    width: int | None
    every: int | None        # None: chosen by `fit`
    quality: int
    at: tuple = ()           # ((hour, minute), ...), in order
    interval: int | None = None
    sun: str | None = None
    sun_offset: int = 0
    tolerance: int = TOLERANCE


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


def _span(value, key, signed=False):
    """A span such as 50h, 90m, 3d, 2w or 1mo (thirty days), in seconds."""
    m = _SPAN.match(value) if isinstance(value, str) else None
    if m is None or (m.group(1) and not signed):
        example = "+30m or -1h" if signed else "50h, 90m, 3d, 2w or 1mo"
        raise SpecError(f"{key} must be a span such as {example}, not {value!r}")
    seconds = int(m.group(2)) * _UNITS[m.group(3)]
    if seconds == 0 and not signed:
        raise SpecError(f"{key} must be longer than nothing, not {value!r}")
    return -seconds if m.group(1) == "-" else seconds


def _clock(value):
    items = [v.strip() for v in value.split(",")] if isinstance(value, str) else value
    if not isinstance(items, list) or not items:
        raise SpecError(f"at must be times of day such as 06:00 or 06:00, 18:00, not {value!r}")
    times = set()
    for item in items:
        m = _CLOCK.match(item) if isinstance(item, str) else None
        if m is None:
            raise SpecError(f"at must be times of day such as 06:00, not {item!r}")
        times.add((int(m.group(1)), int(m.group(2))))
    return tuple(sorted(times))


def _picks(value):
    """The `at`, `interval` and `sun` keys: at most one of them, and what goes with it."""
    given = [k for k in _PICKS if k in value]
    if len(given) > 1:
        raise SpecError(f"give one of at, interval and sun, not {given[1]} as well")
    if "sun_offset" in value and "sun" not in value:
        raise SpecError("sun_offset goes with sun")
    if "tolerance" in value and not given:
        raise SpecError("tolerance goes with at, interval or sun")
    picks = {"at": (), "interval": None, "sun": None, "sun_offset": 0, "tolerance": TOLERANCE}
    if "at" in value:
        picks["at"] = _clock(value["at"])
    if "interval" in value:
        picks["interval"] = _span(value["interval"], "interval")
        picks["tolerance"] = min(TOLERANCE, picks["interval"] // 2)
    if "sun" in value:
        if value["sun"] not in SUN_EVENTS:
            raise SpecError(f"sun must be one of {list(SUN_EVENTS)}, not {value['sun']!r}")
        picks["sun"] = value["sun"]
        if "sun_offset" in value:
            picks["sun_offset"] = _span(value["sun_offset"], "sun_offset", signed=True)
    if "tolerance" in value:
        picks["tolerance"] = _span(value["tolerance"], "tolerance")
    return picks


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
    spans = [k for k in ("range", "duration", "start") if k in value]
    if len(spans) != 1:
        raise SpecError("give one of range (12h, 24h, 7d or 1mo), duration or start, not "
                        + (f"{spans[1]} as well" if spans else "none of them"))
    name = value.get("name")
    rng = None
    if spans[0] == "range":
        rng = value["range"]
        if rng not in RANGES:
            raise SpecError(f"range must be one of {list(RANGES)}, not {rng!r}")
        if name is not None or "end" in value:
            raise SpecError(f"a range build is always named {RESERVED}{rng} and ends now; "
                            "give a duration or a start for a span of your own")
        start, end, kind, name = now - RANGES[rng], now, "range", f"{RESERVED}{rng}"
    else:
        end = _time(value["end"], "end") if "end" in value else now
        if spans[0] == "duration":
            start = end - _span(value["duration"], "duration")
        else:
            start = _time(value["start"], "start")
        if start >= end:
            raise SpecError("start must be before end")
        # A duration ending now is rebuilt under its own name, like a range.
        rolling = spans[0] == "duration" and "end" not in value and name is None
        kind = "range" if rolling else "custom"
        if rolling:
            name = f"{RESERVED}{value['duration']}"
        elif name is None:
            name = f"custom_{_stamp(start)}_{_stamp(end)}"
        elif not isinstance(name, str) or not NAME.match(name):
            raise SpecError(f"name must be 1-48 of a-z, 0-9, _ and -, not {name!r}")
        elif name.startswith(RESERVED):
            raise SpecError(f"name may not start with {RESERVED!r}: those are rebuilt "
                            "rolling spans")
    return Spec(name=name, kind=kind, range=rng, start=start, end=end, fps=ints["fps"],
                width=ints["width"], every=ints["every"], quality=ints["quality"],
                **_picks(value))


def parse_delete(value):
    """A delete request's name, or SpecError."""
    if not isinstance(value, dict) or set(value) != {"name"}:
        raise SpecError(f'a delete is a JSON object {{"name": ...}}, not {value!r}')
    name = value["name"]
    if not isinstance(name, str) or not NAME.match(name):
        raise SpecError(f"name must be 1-48 of a-z, 0-9, _ and -, not {name!r}")
    return name


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


def _days(start, end):
    """Each local calendar day the span touches, as (year, month, day, midnight)."""
    day = time.mktime(time.localtime(start)[:3] + (0, 0, 0, 0, 0, -1))
    while day <= end:
        lt = time.localtime(day)
        yield lt.tm_year, lt.tm_mon, lt.tm_mday, day
        # The next local midnight, whatever the clocks did in between.
        nxt = time.localtime(day + 26 * 3600)
        day = time.mktime(nxt[:3] + (0, 0, 0, 0, 0, -1))


def _archived(root, start, end):
    """(time, path) of every archived still from `start` to `end`, oldest first.
    Only the day folders the span touches are read."""
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
    return sorted(found)


def _too_many(picked, total):
    if len(picked) > MAX_FRAMES:
        fits = -(-total // MAX_FRAMES)
        raise SpecError(f"{len(picked)} stills is more than {MAX_FRAMES}; "
                        f"use every {fits} or a shorter span")
    return picked


def select_stills(root, start, end, every):
    """The archive's stills from `start` to `end` inclusive, oldest first, keeping
    one in every `every` from the first."""
    found = _archived(root, start, end)
    return _too_many([p for _, p in found][::every], len(found))


def _targets(spec, location):
    """The times the picks are nearest to, in order."""
    if spec.interval:
        return [spec.start + k * spec.interval
                for k in range(int((spec.end - spec.start) // spec.interval) + 1)]
    out = []
    for y, mo, d, _ in _days(spec.start, spec.end):
        if spec.at:
            out += [time.mktime((y, mo, d, h, mi, 0, 0, 0, -1)) for h, mi in spec.at]
        else:
            if location is None:
                raise SpecError("sun picks need the camera's latitude and longitude: set "
                                "the latitude and longitude controls")
            t = sun_event(y, mo, d, *location, spec.sun)
            if t is not None:
                out.append(t + spec.sun_offset)
    return [t for t in out if spec.start <= t <= spec.end]


def pick_stills(spec, root, location=None):
    """The stills a timelapse uses, oldest first: every archived still in the span, or
    the one nearest each of the spec's times (at, interval, sun) within its
    tolerance, then one in every `every` if the spec gives it. `location` is
    (latitude, longitude), needed for sun picks."""
    found = _archived(root, spec.start, spec.end)
    if spec.at or spec.interval or spec.sun:
        times = [t for t, _ in found]
        picked, used = [], set()
        for target in _targets(spec, location):
            i = bisect.bisect_left(times, target)
            near = min((j for j in (i - 1, i) if 0 <= j < len(times)),
                       key=lambda j: abs(times[j] - target), default=None)
            close = near is not None and abs(times[near] - target) <= spec.tolerance
            if close and near not in used:
                used.add(near)
                picked.append(found[near][1])
    else:
        picked = [p for _, p in found]
    if spec.every:
        return _too_many(picked[::spec.every], len(picked))
    return picked


# -- how fast ----------------------------------------------------------------------

# Will (2026-10-02): "the priority should be keeping length around 10s, and then
# minimum fps of 20fps", up to 30.
TARGET_S, MIN_FPS, MAX_FPS = 10, 20, 30
LENGTH = (8, 12)            # what "around ten seconds" allows when frames are whole


def fit(n, fps=None, every=None):
    """(every, fps) for `n` stills: about ten seconds of video first, then at least
    twenty frames a second where there are stills enough, up to thirty. Fewer than
    160 stills cannot make ten seconds at twenty, so they play slower, for exactly
    ten. A given `fps` or `every` is kept and the other fitted to it."""
    if fps is not None and every is not None:
        return every, fps
    if fps is not None:
        e = 1
        while -(-n // e) / fps > LENGTH[1]:
            e += 1
        return e, fps
    if every is not None:
        m = -(-n // every)
        return every, _fps(m)
    best = None
    for e in range(1, max(1, n // (MIN_FPS * LENGTH[0])) + 1):
        m = -(-n // e)
        f = min(MAX_FPS, max(MIN_FPS, round(m / TARGET_S)))
        if LENGTH[0] <= m / f <= LENGTH[1]:
            score = (abs(m / f - TARGET_S), e)
            if best is None or score < best[0]:
                best = (score, (e, f))
    return best[1] if best else (1, _fps(n))


def _fps(m):
    """Frames a second for `m` frames on their own: whole, 20-30, where ten seconds
    allows; otherwise to a tenth, for ten seconds exactly."""
    if m >= MIN_FPS * LENGTH[0]:
        return min(MAX_FPS, max(MIN_FPS, round(m / TARGET_S)))
    return max(0.1, round(m / TARGET_S, 1))


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
    `<spec.name>.json`, both replaced only once complete. Returns the sidecar.
    `spec.fps` must be decided: TimelapseBuilder fits it before it encodes."""
    if spec.fps is None:
        raise ValueError("encode needs a spec whose fps is decided; see fit()")
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


# -- the builder --------------------------------------------------------------------

# Every value sensor.timelapse can take: what the last request came to.
TIMELAPSE_STATES = ("idle", "queued", "building", "built", "deleted", "rejected_invalid",
                    "rejected_busy", "rejected_duplicate", "rejected_unknown",
                    "failed_no_stills", "failed_encoder", "off")
# Builds waiting behind the one running. More is refused: a burst of requests is a
# misfiring automation, not a person.
MAX_WAITING = 4
_TIMES = ("start", "end", "first", "last", "built")


def _iso(t):
    return datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds")


def _entry(meta):
    """A sidecar as the catalogue publishes it: times readable, the spec left out,
    and where to fetch it."""
    out = {k: v for k, v in meta.items() if k != "spec"}
    for k in _TIMES:
        if isinstance(out.get(k), int | float):
            out[k] = _iso(out[k])
    out["url"] = media_path(meta["name"])
    return out


class TimelapseBuilder:
    """Builds timelapses one at a time on a thread of its own.

    `publish(d)` receives state keys -- `timelapse`, `timelapse_progress`, the
    count `timelapses`, the catalogue `timelapse_catalogue` ({"items": {name: ...}})
    and the last outcome with its reason `timelapse_last` -- and `event(d)` one event per
    outcome, `{ts, control, name, result, reason}`: the supervisor hands both on to
    MQTT, the API and the page alike.
    """

    def __init__(self, out_dir, archive_root, publish, event, ffmpeg="ffmpeg", clock=time.time,
                 location=lambda: None):
        self.out_dir, self.archive_root, self.ffmpeg = out_dir, archive_root, ffmpeg
        # (latitude, longitude) for sun picks, or None; asked at each build, since
        # the controls can change.
        self._location = location
        self._publish, self._event, self._clock = publish, event, clock
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        # Held from taking a snapshot to publishing it, so what is published is
        # always in the order it happened: without it the worker could publish
        # "building" and "built" while a "queued" taken earlier was on its way,
        # and the sensor would end on "queued". Always taken before `_lock`.
        self._publishing = threading.RLock()
        self._last = {"name": None, "control": None, "result": "idle", "reason": None,
                      "ts": None}
        self._waiting = collections.deque()
        self._building = None
        self._catalogue = load_catalogue(out_dir)
        self._state, self._progress = "idle", 0
        self._thread, self._stopping = None, False

    # -- what it publishes --

    def state(self):
        with self._lock:
            return self._state_locked()

    def _state_locked(self):
        # The catalogue under one key: its names are chosen by whoever builds, and
        # as attributes of their own a timelapse called `icon` would be one.
        return {"timelapse": self._state, "timelapse_progress": self._progress,
                "timelapses": len(self._catalogue),
                "timelapse_catalogue": {"items": {n: _entry(m)
                                                  for n, m in sorted(self._catalogue.items())}},
                "timelapse_last": dict(self._last)}

    def _outcome(self, name, state, reason=None, control="build_timelapse"):
        """Say what a request came to: as state -- including the reason, since with
        MQTT off there is no event channel at all -- and as an event."""
        now = self._clock()
        with self._publishing:
            with self._lock:
                self._state = state
                self._last = {"name": name, "control": control, "result": state,
                              "reason": reason, "ts": _iso(now)}
                snapshot = self._state_locked()
            (log.info if reason is None else log.warning)(
                "timelapse %s: %s%s", name, state, f" ({reason})" if reason else "")
            self._publish(snapshot)
        self._event({"ts": now, "control": control, "name": name,
                     "result": state, "reason": reason})
        return state

    def _on_progress(self, done, total):
        pct = done * 100 // total if total else 100
        with self._publishing:
            with self._lock:
                if pct == self._progress:
                    return
                self._progress = pct
            self._publish({"timelapse_progress": pct})

    # -- requests --

    def refuse(self, name, reason, control="build_timelapse"):
        """Say a request was refused before it reached the builder -- a spec MQTT
        delivered that does not parse -- the way the builder says everything else."""
        return self._outcome(name, "rejected_invalid", reason, control=control)

    def request(self, spec):
        """Queue a build. Returns the state it left: queued, or why it was refused.
        The worker cannot say "building" before this has said "queued"."""
        with self._publishing:
            with self._lock:
                duplicate = spec.name == self._building or any(
                    s.name == spec.name for s in self._waiting)
                full = len(self._waiting) >= MAX_WAITING
                if not duplicate and not full:
                    self._waiting.append(spec)
                    self._wake.notify()
            if duplicate:
                return self._outcome(spec.name, "rejected_duplicate",
                                     "a build of that name is already waiting or running")
            if full:
                return self._outcome(spec.name, "rejected_busy",
                                     f"{MAX_WAITING} builds are already waiting")
            return self._outcome(spec.name, "queued")

    def delete(self, name):
        with self._lock:
            known = isinstance(name, str) and NAME.match(name) and name in self._catalogue
            busy = name == self._building
        if not known:
            return self._outcome(str(name), "rejected_unknown",
                                 "there is no timelapse of that name", control="delete_timelapse")
        if busy:
            return self._outcome(name, "rejected_busy", "it is being rebuilt",
                                 control="delete_timelapse")
        delete(self.out_dir, name)
        with self._lock:
            self._catalogue.pop(name, None)
        return self._outcome(name, "deleted", control="delete_timelapse")

    # -- building --

    def run_once(self):
        """Build the next waiting request on this thread. Returns its outcome, or None
        with nothing waiting."""
        with self._lock:
            if not self._waiting:
                return None
            spec = self._waiting.popleft()
            self._building, self._progress = spec.name, 0
        try:
            self._outcome(spec.name, "building")
            try:
                stills = pick_stills(spec, self.archive_root, self._location())
            except SpecError as e:
                return self._outcome(spec.name, "rejected_invalid", str(e))
            if not stills:
                return self._outcome(spec.name, "failed_no_stills",
                                     "the archive has no stills in that span"
                                     + (" near those times" if spec.at or spec.interval
                                        or spec.sun else ""))
            # About ten seconds, unless the spec says otherwise: `every` was already
            # applied by pick_stills if given, so only what is left is fitted.
            step, fps = fit(len(stills), spec.fps, 1 if spec.every else None)
            stills = stills[::step]
            spec = replace(spec, fps=fps, every=(spec.every or 1) * step)
            try:
                meta = encode(stills, spec, self.out_dir, progress=self._on_progress,
                              ffmpeg=self.ffmpeg)
            except EncodeError as e:
                return self._outcome(spec.name, "failed_encoder", str(e))
            except Exception as e:
                log.exception("timelapse %s: the build failed", spec.name)
                return self._outcome(spec.name, "failed_encoder", repr(e))
            with self._lock:
                self._catalogue[spec.name] = meta
            return self._outcome(spec.name, "built")
        finally:
            with self._lock:
                self._building = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="timelapse", daemon=True)
        self._thread.start()

    def stop(self, timeout=5.0):
        with self._lock:
            self._stopping = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self):
        while True:
            with self._lock:
                while not self._waiting and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
            self.run_once()
