"""Runtime settings: defaults, environment overrides, and Home Assistant control writes."""

from dataclasses import dataclass

# Control names Home Assistant may write. Each must name a real Settings field;
# tests/test_settings.py enforces that, because a control naming no field would
# accept writes that silently go nowhere.
CONTROLS = {
    "interval_s", "stacking_mode", "stacking_allowed", "fixed_depth",
    "target_level", "subject_percentile", "clean_factor", "max_depth",
    "window_max_s", "ramp_tau_s", "black_level_override", "white_balance",
    "render_intent", "night_key", "stretch_strength", "chroma_radius",
    "denoise_strength", "autofocus",
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
    "MQTT_HOST": "mqtt_host",
    "MQTT_PORT": "mqtt_port",
    "MQTT_USER": "mqtt_user",
    "MQTT_PASSWORD": "mqtt_password",
    "MQTT_PREFIX": "mqtt_prefix",
    "ARCHIVE_DIR": "archive_dir",
    "LIVE_PORT": "live_port",
}

_TRUTHY = {"1", "true", "yes", "on"}

# Controls with a fixed set of values. Home Assistant's select entity keeps a
# person to these, but a typo from an automation or mosquitto_pub would
# otherwise be accepted and then silently fall back to a default mid-render.
_CHOICES = {
    "stacking_mode": ("off", "fixed", "auto"),
    "white_balance": ("as-shot", "grey-world", "brightest-patch"),
    "render_intent": ("natural", "inspection"),
}


@dataclass
class Settings:
    phone_host: str = "192.168.1.11"
    phone_port: int = 8080
    phone_user: str = "root"
    phone_password: str = ""
    mqtt_host: str = "192.168.1.5"
    mqtt_port: int = 1883
    mqtt_user: str = "ios2ha"
    mqtt_password: str = ""
    mqtt_prefix: str = "ios2ha_camera"
    archive_dir: str = "/data/archive"
    live_port: int = 8099
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

    @classmethod
    def from_env(cls, env):
        s = cls()
        for key, attr in _ENV_MAP.items():
            if key not in env:
                continue
            setattr(s, attr, _coerce_like(getattr(s, attr), env[key]))
        return s

    def apply_control(self, name, value):
        """Apply a Home Assistant control write. Returns whether the name was known."""
        channel = BLACK_LEVEL_CHANNELS.get(name)
        if channel is not None:
            current = self.black_level_override or (0.0, 0.0, 0.0)
            updated = list(current)
            updated[channel] = float(value)
            self.black_level_override = tuple(updated)
            return True
        if name not in CONTROLS:
            return False
        if name in _CHOICES and value not in _CHOICES[name]:
            raise ValueError(f"{name} must be one of {_CHOICES[name]}, not {value!r}")
        if name == "black_level_override" and value is not None:
            value = tuple(float(v) for v in value)
        setattr(self, name, value)
        return True


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
