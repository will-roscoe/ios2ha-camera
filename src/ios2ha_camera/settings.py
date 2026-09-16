"""Runtime settings: defaults, environment overrides, and Home Assistant control writes."""

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

# Control names Home Assistant may write. Each must name a real Settings field;
# tests/test_settings.py enforces that, because a control naming no field would
# accept writes that silently go nowhere.
CONTROLS = {
    "interval_s", "stacking_mode", "stacking_allowed", "fixed_depth",
    "target_level", "subject_percentile", "clean_factor", "max_depth",
    "window_max_s", "ramp_tau_s", "black_level_override", "white_balance",
    "render_intent", "night_key", "stretch_strength", "chroma_radius",
    "denoise_strength", "autofocus", "on_demand", "live_max_window_s",
    "backgrounding", "background_below_pct", "resume_above_pct", "auto_start",
}

# Discovery publishes one number entity per channel, so the per-channel names
# are controls too, writing into a single tuple field.
BLACK_LEVEL_CHANNELS = {"black_level_0": 0, "black_level_1": 1, "black_level_2": 2}

# Environment variable -> field. Values arrive as strings and are coerced to the
# type of the field's default in from_env.
_ENV_MAP = {
    "PHONE_HOST": "phone_host",
    "PHONE_PORT": "phone_port",
    "PHONE_PASSWORD": "phone_password",
    "MQTT_URL": "mqtt_url",
    "MQTT_HOST": "mqtt_host",
    "MQTT_PORT": "mqtt_port",
    "MQTT_USER": "mqtt_user",
    "MQTT_PASSWORD": "mqtt_password",
    "MQTT_PREFIX": "mqtt_prefix",
    "ARCHIVE_DIR": "archive_dir",
    "LIVE_PORT": "live_port",
    "EXPOSE_MQTT": "expose_mqtt",
    "EXPOSE_API": "expose_api",
}

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

# EXPOSE_MQTT / EXPOSE_API get their own coercion (see _coerce_expose_flag)
# rather than _coerce_like's blanket "anything not in _TRUTHY is False": a typo
# here must not silently switch an interface off, only every other boolean env
# var keeps that behaviour.
_EXPOSE_ENV_KEYS = {"EXPOSE_MQTT", "EXPOSE_API"}

# Controls with a fixed set of values. Home Assistant's select entity keeps a
# person to these, but a typo from an automation or mosquitto_pub would
# otherwise be accepted and then silently fall back to a default mid-render.
_CHOICES = {
    "stacking_mode": ("off", "fixed", "auto"),
    "white_balance": ("as-shot", "grey-world", "brightest-patch"),
    "render_intent": ("natural", "inspection"),
}


def _coerce_bool(value):
    """Coerce a Home Assistant boolean control write to a real bool.

    Home Assistant's own switch UI always sends the JSON `true`/`false` it is
    configured with (mqtt_io.py's payload_on/payload_off), which json.loads has
    already turned into an actual bool before apply_control ever sees it -- the
    isinstance check below is what makes that case a no-op. This exists for the
    non-UI publisher (a script, Node-RED, a hand-typed mosquitto_pub) sending a
    conventional-looking value that is not valid JSON, e.g. `off`: json.loads
    fails on it, mqtt_io falls back to the raw string, and a bare setattr used to
    store that string as-is -- `not "off"` is False, the opposite of what was
    sent. Anything outside these spellings is rejected rather than guessed at.
    """
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    raise ValueError(f"cannot interpret {value!r} as a boolean")


@dataclass
class Settings:
    phone_host: str = "192.168.1.11"
    phone_port: int = 8080
    phone_user: str = "root"
    phone_password: str = ""
    # One or more space-separated broker URLs; see parse_mqtt_urls. When empty,
    # the deprecated mqtt_host / mqtt_port below say the same thing.
    mqtt_url: str = ""
    mqtt_host: str = "192.168.1.5"
    mqtt_port: int = 1883
    mqtt_user: str = "ios2ha"
    mqtt_password: str = ""
    mqtt_prefix: str = "ios2ha_camera"
    archive_dir: str = "/data/archive"
    live_port: int = 8099
    # Which interfaces reach Home Assistant. Media routes are always served --
    # they are the camera -- so turning both off is a loud warning, not a
    # refusal: stills are still captured and archived. Not in CONTROLS: these
    # pick which network surface exists at all, not something to flip live.
    expose_mqtt: bool = True
    expose_api: bool = True
    interval_s: int = 600
    stacking_mode: str = "auto"          # off | fixed | auto
    stacking_allowed: bool = True
    fixed_depth: int = 30
    target_level: float = 45.0           # 0..255 after black level
    subject_percentile: float = 10.0
    clean_factor: float = 4.0            # k in the depth law
    max_depth: int = 600
    window_max_s: float = 60.0
    ramp_tau_s: float = 900.0
    black_level_override: tuple | None = None
    white_balance: str = "as-shot"       # as-shot | grey-world | brightest-patch
    render_intent: str = "natural"       # natural | inspection
    night_key: float = 0.09
    stretch_strength: float = 25.0
    chroma_radius: float = 3.0
    denoise_strength: float = 3.0
    autofocus: bool = True
    on_demand: bool = True               # False keeps the connection open continuously
    live_max_window_s: float = 3.0       # live feed's depth cap, in seconds of real time
    # Backgrounding between captures. iOS suspends a backgrounded app's listening
    # socket while leaving the process alive, which is worth roughly 370 mA:
    # measured 2026-09-13, the phone went from 30% to 96% in under three hours
    # backgrounded, where streaming barely breaks even on this 3 W port.
    backgrounding: bool = True
    # Hysteresis, so a phone hovering at the threshold does not thrash between
    # foreground and background. Only engages when the battery is genuinely low,
    # and stops once it has recovered a real margin.
    background_below_pct: float = 50.0
    resume_above_pct: float = 80.0
    # Whether the supervisor may press Start streaming itself when the app is
    # open but idle. Off leaves a stream stopped by hand on the phone stopped;
    # the Home Assistant button still works, because a person is asking.
    auto_start: bool = True

    @classmethod
    def from_env(cls, env):
        s = cls()
        for key, attr in _ENV_MAP.items():
            if key not in env:
                continue
            if key in _EXPOSE_ENV_KEYS:
                setattr(s, attr, _coerce_expose_flag(key, env[key], getattr(s, attr)))
            else:
                setattr(s, attr, _coerce_like(getattr(s, attr), env[key]))
        # Remembered so mqtt_endpoints() can name which deprecated variables an
        # MQTT_URL overrode. Not a dataclass field: it is provenance, not a setting.
        s._mqtt_alias_env = tuple(k for k in ("MQTT_HOST", "MQTT_PORT") if k in env)
        return s

    def apply_control(self, name, value):
        """Apply a Home Assistant control write. Returns whether the name was known."""
        channel = BLACK_LEVEL_CHANNELS.get(name)
        if channel is not None:
            current = self.black_level_override or (0.0, 0.0, 0.0)
            updated = list(current)
            try:
                updated[channel] = float(value)
            except TypeError as e:
                # None, a list, an object: float() says TypeError, which the
                # supervisor's corrective republish does not catch. A string
                # that is not a number is already a ValueError.
                raise ValueError(f"{name} must be a number, not {value!r}") from e
            self.black_level_override = tuple(updated)
            return True
        if name not in CONTROLS:
            return False
        if name in _CHOICES and value not in _CHOICES[name]:
            raise ValueError(f"{name} must be one of {_CHOICES[name]}, not {value!r}")
        if name in _BOOL_CONTROLS:
            value = _coerce_bool(value)
        if name == "black_level_override" and value is not None:
            try:
                value = tuple(float(v) for v in value)
            except TypeError as e:
                # A bare number (or anything else non-iterable) instead of a
                # 3-tuple. Supervisor._apply_setting only catches ValueError to
                # log "rejected control" and republish the true value -- a
                # TypeError here used to skip that corrective path entirely and
                # leave Home Assistant showing the rejected write.
                raise ValueError(
                    f"black_level_override must be an iterable of numbers, not {value!r}"
                ) from e
        setattr(self, name, value)
        return True


# Boolean controls, derived from the dataclass itself: exactly the CONTROLS
# fields whose default is an actual bool. Kept off a hand-typed list so it
# cannot silently drift from the dataclass as fields are added or retyped.
# Keyed off the default value rather than `f.type`, for the same reason as
# _coerce_like: under postponed annotations `f.type` is the string "bool", and
# the set would come out empty with nothing to say so.
_BOOL_CONTROLS = {
    f.name for f in Settings.__dataclass_fields__.values()
    if isinstance(f.default, bool)
} & CONTROLS


@dataclass(frozen=True)
class MqttEndpoint:
    """One broker address, with what its scheme implies about how to reach it."""

    scheme: str
    host: str
    port: int
    transport: str      # "tcp" or "websockets", as paho's Client(transport=...) takes it
    tls: bool
    path: str           # the websocket path; unused over tcp


# scheme -> (default port, paho transport, TLS). The scheme carries all three, so there
# is no matrix of MQTT_PORT / MQTT_TLS / MQTT_TRANSPORT variables to keep consistent.
_MQTT_SCHEMES = {
    "mqtt": (1883, "tcp", False),
    "mqtts": (8883, "tcp", True),
    "ws": (80, "websockets", False),
    "wss": (443, "websockets", True),
}


def parse_mqtt_urls(value):
    """Parse MQTT_URL: one or more space-separated broker URLs, tried in order.

    Credentials in a URL are refused: anything that quotes the URL, an error or a
    log line, would print the password, so they belong in MQTT_USER and
    MQTT_PASSWORD. A list mixing schemes is refused too: paho fixes the transport
    when the client is built and TLS before connecting, so a fallback cannot move
    between them. Errors name the entry by position, never by its text.
    """
    entries = value.split()
    if not entries:
        raise ValueError("MQTT_URL is set but empty")
    endpoints = []
    for n, raw in enumerate(entries, 1):
        try:
            u = urlsplit(raw)
            port = u.port
        except ValueError:
            raise ValueError(f"MQTT_URL entry {n} is not a valid URL") from None
        if u.scheme not in _MQTT_SCHEMES:
            raise ValueError(f"MQTT_URL entry {n} must use one of "
                             f"{', '.join(s + '://' for s in _MQTT_SCHEMES)}")
        if u.username is not None or u.password is not None:
            raise ValueError(f"MQTT_URL entry {n} embeds credentials; put them in "
                             "MQTT_USER and MQTT_PASSWORD instead")
        if not u.hostname:
            raise ValueError(f"MQTT_URL entry {n} has no host")
        default_port, transport, tls = _MQTT_SCHEMES[u.scheme]
        endpoints.append(MqttEndpoint(u.scheme, u.hostname, port or default_port,
                                      transport, tls, u.path or "/mqtt"))
    if len({e.scheme for e in endpoints}) > 1:
        raise ValueError("MQTT_URL entries must all use the same scheme: paho fixes the "
                         "transport and TLS when the client is created")
    return endpoints


def mqtt_endpoints(settings):
    """The brokers to try, in order.

    MQTT_URL wins. MQTT_HOST and MQTT_PORT are the deprecated way to say the same
    thing and keep working on their own without complaint, since a deployment that
    has not migrated is not a fault; a warning appears only when both are set and
    one is being ignored.
    """
    if settings.mqtt_url:
        ignored = getattr(settings, "_mqtt_alias_env", ())
        if ignored:
            log.warning("MQTT_URL is set, so %s %s ignored", " and ".join(ignored),
                        "is" if len(ignored) == 1 else "are")
        return parse_mqtt_urls(settings.mqtt_url)
    return [MqttEndpoint("mqtt", settings.mqtt_host, settings.mqtt_port, "tcp", False, "/mqtt")]


def _coerce_expose_flag(name, raw, default):
    """Coerce EXPOSE_MQTT / EXPOSE_API: conventional spellings only, and an
    unrecognised value keeps the default rather than being guessed at.

    _coerce_like treats anything outside _TRUTHY as False, which would make
    EXPOSE_MQTT=ture switch MQTT off without a word -- a typo silently
    disabling Home Assistant discovery, with nothing in the log to explain
    why entities vanished. Nothing optional may stop the container starting,
    so this warns and carries on rather than raising.
    """
    lowered = str(raw).strip().lower()
    if lowered in _TRUTHY:
        return True
    if lowered in _FALSY:
        return False
    log.warning("%s=%r is not true/false; keeping the default (%s)", name, raw, default)
    return default


def both_interfaces_off_message():
    """The one warning to log when EXPOSE_MQTT and EXPOSE_API are both false.

    Returned rather than logged here so startup code in `__main__`, which is
    where both flags are read together, decides when to log it -- Settings
    construction runs before that check is meaningful.
    """
    return (
        "EXPOSE_MQTT and EXPOSE_API are both false: no Home Assistant discovery "
        "and no JSON API. Nothing else changes: capture, stacking and archive "
        "continue, and /live, /live/stacked and /still.jpg keep serving."
    )


def _coerce_like(default, raw):
    """Coerce an environment string to the type of the field's default value.

    Keyed off the default rather than the annotation so it stays correct whether
    or not annotations are evaluated lazily.
    """
    if isinstance(raw, type(default)) and not isinstance(default, bool):
        return raw
    if isinstance(default, bool):
        return str(raw).strip().lower() in _TRUTHY
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw
