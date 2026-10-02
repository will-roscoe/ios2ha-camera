"""Sunrise, sunset and solar noon for a day and a place.

For picking a timelapse's stills by the sun ("a month of sunrises"). The sunrise
equation (as NOAA's and Wikipedia's give it) is accurate to about a minute, which is
far finer than a still every ten minutes needs, and needs no dependency.
"""

import calendar
import math

_J1970 = 2440587.5          # the Julian date of the Unix epoch
_J2000 = 2451545.0
_EVENTS = ("sunrise", "sunset", "noon")


def _sin(deg):
    return math.sin(math.radians(deg))


def _cos(deg):
    return math.cos(math.radians(deg))


def sun_event(year, month, day, latitude, longitude, event):
    """When `event` happens on that calendar day at that place, as a Unix time; None
    when the sun does not rise or set there that day. Longitude is east-positive."""
    if event not in _EVENTS:
        raise ValueError(f"event must be one of {_EVENTS}, not {event!r}")
    noon_utc = calendar.timegm((year, month, day, 12, 0, 0))
    n = math.ceil(noon_utc / 86400 + _J1970 - _J2000 + 0.0008 - 0.5)
    mean = n - longitude / 360                                  # mean solar time
    anomaly = (357.5291 + 0.98560028 * mean) % 360
    centre = 1.9148 * _sin(anomaly) + 0.0200 * _sin(2 * anomaly) + 0.0003 * _sin(3 * anomaly)
    ecliptic = (anomaly + centre + 180 + 102.9372) % 360
    transit = _J2000 + mean + 0.0053 * _sin(anomaly) - 0.0069 * _sin(2 * ecliptic)
    if event == "noon":
        return (transit - _J1970) * 86400
    declination = math.degrees(math.asin(_sin(ecliptic) * _sin(23.4397)))
    cos_hour = ((_sin(-0.833) - _sin(latitude) * _sin(declination))
                / (_cos(latitude) * _cos(declination)))
    if not -1 <= cos_hour <= 1:
        return None                                             # polar day or night
    hour = math.degrees(math.acos(cos_hour)) / 360
    return ((transit - hour if event == "sunrise" else transit + hour) - _J1970) * 86400
