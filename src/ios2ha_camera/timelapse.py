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

import re
import time
from dataclasses import dataclass
from datetime import datetime

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
