"""Every entity this service publishes, declared once.

These 86 records are the whole declaration. `mqtt_io` is one renderer over them,
turning each into a Home Assistant discovery payload; it is not the only intended
one, which is why the declaration is here rather than inside it. A Home Assistant
custom component living in its own repository, on its own release cycle, needs the
same object ids, names, units, device classes, options and ranges, and the only way
it can have them without a second hand-kept copy to drift against is to be served
this one. Adding a renderer is a new reader of this module; it is never a second
place to state what an entity is.

`state_key` is the key in the supervisor's published state this entity reads. Keeping
it here is what lets a test assert that every published value has an entity and every
entity has a publisher.

`writable` means Home Assistant may write to this entity, which is exactly the set
that gets a `command_topic`: the numbers, selects and switches that carry a value, and
the buttons that carry only a press. A button is therefore writable with no
`state_key`, which is the only combination that looks odd and is the honest one.

The two cameras carry `topic` instead of a state key: their payloads are the JPEG and
PNG published to their own topics, not a value under `state/`.
"""

from dataclasses import dataclass, field

from . import version
from .app_open import APP_OPEN_STATES
from .states import STREAM_STATES
from .stream_start import STREAM_START_STATES


def device_info():
    """The device all 86 entities hang off, declared once for both renderers.

    MQTT repeats it inside every discovery payload and the API serves it from
    `/api/v1/info`, because a component built from the API has to describe the
    same physical thing the MQTT entities describe -- same name, same model,
    same running version. Two hand-kept copies of five keys is exactly the drift
    this module exists to remove, one level down.

    `sw_version` puts the running build on the device page in Home Assistant, so
    "what is deployed?" is answerable without shell access. Read on each call,
    as the payload builder always read it, so moving the declaration here cannot
    change what MQTT publishes.
    """
    return {"identifiers": ["ios2ha_camera"], "name": "iPhone Camera",
            "manufacturer": "ios2ha-camera", "model": "iPhone supervisor",
            "sw_version": version.full()}


@dataclass(frozen=True)
class Entity:
    object_id: str
    domain: str
    # None on exactly one entity, the still camera: a null name is how Home
    # Assistant is told this is the device's main feature, so it shows as
    # "iPhone Camera" rather than "iPhone Camera Camera". It is not the same as
    # leaving the key out, and is published as `"name": null`.
    name: str | None
    state_key: str | None = None
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    options: tuple[str, ...] | None = None
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    category: str | None = None
    icon: str | None = None
    writable: bool = False
    # Digits Home Assistant should show, rendered as suggested_display_precision.
    # Every entity carries this field, null where a sensor does not set it, so
    # giving one sensor a precision cannot change the shape of every other
    # descriptor.
    precision: int | None = None
    # The payload topic of a camera entity, relative to the MQTT prefix. Only the
    # cameras use it; everything else derives its topics from `object_id`.
    topic: str | None = None


@dataclass(frozen=True)
class MediaSource:
    id: str
    kind: str                    # "stream" | "snapshot"
    formats: tuple[str, ...]
    path: str
    holds_phone: bool
    params: dict = field(default_factory=dict)


def _camera(object_id, name, topic, icon=None, category=None):
    return Entity(object_id=object_id, domain="camera", name=name, topic=topic,
                  icon=icon, category=category)


def _number(object_id, name, lo, hi, step, unit=None, device_class=None, icon=None):
    return Entity(object_id=object_id, domain="number", name=name, state_key=object_id,
                  lo=lo, hi=hi, step=step, unit=unit, device_class=device_class,
                  icon=icon, category="config", writable=True)


def _select(object_id, name, options):
    return Entity(object_id=object_id, domain="select", name=name, state_key=object_id,
                  options=options, category="config", writable=True)


def _switch(object_id, name, icon):
    return Entity(object_id=object_id, domain="switch", name=name, state_key=object_id,
                  icon=icon, category="config", writable=True)


def _button(object_id, name, icon=None, device_class=None, category=None):
    return Entity(object_id=object_id, domain="button", name=name, icon=icon,
                  device_class=device_class, category=category, writable=True)


def _sensor(object_id, name, **kw):
    return Entity(object_id=object_id, domain="sensor", name=name, state_key=object_id, **kw)


def _binary(object_id, name, device_class=None):
    return Entity(object_id=object_id, domain="binary_sensor", name=name,
                  state_key=object_id, device_class=device_class, category="diagnostic")


def _ordinal(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


# The still is the device's main feature, which is what a null name marks: Home
# Assistant then shows it as "iPhone Camera" rather than "iPhone Camera Camera".
# The screenshot is a distinct entity so the retained PNG the button publishes
# does not collide with the still.
_CAMERAS = [
    _camera("still", None, "still", icon="mdi:camera"),
    _camera("screenshot", "Screenshot", "screenshot", icon="mdi:cellphone-screenshot",
            category="diagnostic"),
]

# Tuning knobs. All configuration rather than measurement, and each carries its
# unit so a person reading "600" knows it is seconds, plus a device class where
# the value has a real physical type.
_NUMBERS = [
    _number("interval_s", "Capture interval", lo=30, hi=3600, step=1, unit="s",
            device_class="duration", icon="mdi:timer-outline"),
    # The hysteresis band for backgrounding. Two numbers rather than one so a
    # phone sitting at the threshold cannot thrash: it engages low and only
    # stops once the battery has recovered a real margin.
    _number("background_below_pct", "Background below", lo=0, hi=100, step=1,
            unit="%", device_class="battery", icon="mdi:battery-arrow-down"),
    _number("resume_above_pct", "Resume above", lo=0, hi=100, step=1,
            unit="%", device_class="battery", icon="mdi:battery-arrow-up"),
    _number("fixed_depth", "Fixed depth", lo=1, hi=1000, step=1, unit="frames",
            icon="mdi:layers-triple"),
    _number("target_level", "Target subject level", lo=1, hi=255, step=1,
            icon="mdi:brightness-4"),
    _number("subject_percentile", "Subject percentile", lo=1, hi=50, step=1, unit="%"),
    _number("clean_factor", "Clean factor k", lo=1, hi=64, step=1, icon="mdi:grain"),
    _number("max_depth", "Maximum depth", lo=1, hi=2000, step=1, unit="frames",
            icon="mdi:layers-outline"),
    _number("window_max_s", "Maximum capture window", lo=1, hi=120, step=1, unit="s",
            device_class="duration", icon="mdi:timer-sand"),
    _number("ramp_tau_s", "Ramp time constant", lo=0, hi=7200, step=10, unit="s",
            device_class="duration", icon="mdi:chart-bell-curve"),
    _number("night_key", "Night key", lo=0.01, hi=0.5, step=0.01, icon="mdi:weather-night"),
    _number("stretch_strength", "Stretch strength", lo=1, hi=60, step=1,
            icon="mdi:arrow-expand-vertical"),
    _number("chroma_radius", "Chroma smoothing radius", lo=0, hi=12, step=0.5, unit="px",
            icon="mdi:blur"),
    _number("denoise_strength", "Denoise strength", lo=0, hi=20, step=0.5,
            icon="mdi:blur-off"),
    _number("black_level_0", "Black level override R", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("black_level_1", "Black level override G", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("black_level_2", "Black level override B", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("live_max_window_s", "Stacked feed window", lo=0.5, hi=15, step=0.5, unit="s",
            device_class="duration", icon="mdi:motion-outline"),
]

# The options are the vocabulary Settings.apply_control will accept; a select
# offering anything else is a control Home Assistant can send and the service
# will reject, which tests/test_entities.py asserts against settings._CHOICES.
_SELECTS = [
    _select("stacking_mode", "Stacking mode", ("off", "fixed", "auto")),
    _select("white_balance", "White balance", ("as-shot", "grey-world", "brightest-patch")),
    _select("render_intent", "Render intent", ("natural", "inspection")),
]

_SWITCHES = [
    _switch("stacking_allowed", "Stacking allowed", "mdi:layers"),
    _switch("autofocus", "Autofocus", "mdi:image-filter-center-focus-strong"),
    _switch("on_demand", "On demand", "mdi:power-plug-off-outline"),
    _switch("backgrounding", "Backgrounding", "mdi:cellphone-arrow-down"),
    _switch("auto_start", "Auto start streaming", "mdi:play-box-outline"),
]

# capture_now stays uncategorised: it is the one button that belongs on a
# dashboard rather than buried under configuration or diagnostics.
_BUTTONS = [
    _button("capture_now", "Capture now", icon="mdi:camera-iris"),
    _button("screenshot", "Phone screenshot", icon="mdi:cellphone-screenshot",
            category="diagnostic"),
    _button("relaunch_app", "Relaunch camera app", device_class="restart",
            category="config"),
    _button("prepare_phone", "Prepare phone", icon="mdi:cellphone-cog",
            category="config"),
    # Presses Start streaming now, ignoring the pause switch and the rate
    # limit: a person asking is an explicit instruction. The guards still hold.
    _button("start_streaming", "Start streaming", icon="mdi:video-check",
            category="config"),
]

# Sensor metadata. Without a unit, a device class and a state class, Home
# Assistant cannot say what a reading means, cannot graph it sensibly and keeps
# no long-term statistics for it -- "battery_temperature = 35.39" of what?
# `category="diagnostic"` keeps the thirty-odd measurements out of the main card
# while leaving them fully usable in automations and dashboards.
# Luminance figures are relative 0-255 after black-level subtraction, not lux,
# so they deliberately carry no unit; see the design's section 4.3.
_SENSORS = [
    # Options come from states.STREAM_STATES rather than a second copy: Home
    # Assistant rejects a state that is not in this list, so the two drifting
    # apart shows up as an entity that silently goes unavailable.
    _sensor("stream_state", "Stream state", device_class="enum",
            options=tuple(STREAM_STATES), icon="mdi:video-wireless"),
    # The USB side, which is independent of the WiFi stream above. "no_device" is
    # the cable being out, which is ordinary; "usbmuxd_down" means the daemon is
    # unreachable and the socket mount is presumed orphaned, which is a fault and
    # is what the offline alert should watch.
    _sensor("cable_state", "Cable state", device_class="enum",
            options=("ok", "no_device", "usbmuxd_down"), icon="mdi:usb-port",
            category="diagnostic"),
    _sensor("subject_level", "Subject level", state_class="measurement", precision=1,
            icon="mdi:brightness-4"),
    _sensor("depth_used", "Depth used", unit="frames", state_class="measurement",
            icon="mdi:layers-triple"),
    _sensor("motion", "Motion level", state_class="measurement", precision=2,
            icon="mdi:motion-sensor"),
    _sensor("frame_rate", "Frame rate", unit="fps", state_class="measurement",
            precision=1, category="diagnostic", icon="mdi:speedometer"),
    # The time of the last frame, not an age in seconds: an age is only true at
    # the instant it is published and is wrong by up to a health tick
    # afterwards, whereas Home Assistant renders a timestamp as a live relative
    # age that stays correct on its own.
    _sensor("last_frame", "Last frame", device_class="timestamp",
            category="diagnostic", icon="mdi:clock-outline"),
    _sensor("depth_required", "Depth required before clamping", unit="frames",
            state_class="measurement", category="diagnostic", icon="mdi:layers-outline"),
    _sensor("capture_duration", "Capture duration", unit="s", device_class="duration",
            state_class="measurement", precision=1, category="diagnostic"),
    _sensor("colour_temperature", "Colour temperature", unit="K",
            state_class="measurement", category="diagnostic", icon="mdi:temperature-kelvin"),
    _sensor("effective_exposure_required", "Effective exposure required", unit="s",
            device_class="duration", state_class="measurement", precision=2,
            category="diagnostic"),
    _sensor("effective_exposure_achieved", "Effective exposure achieved", unit="s",
            device_class="duration", state_class="measurement", precision=2,
            category="diagnostic"),
    _sensor("lum_mean", "Scene luminance mean", state_class="measurement", precision=1,
            category="diagnostic", icon="mdi:brightness-6"),
    _sensor("dynamic_range", "Dynamic range", state_class="measurement", precision=1,
            category="diagnostic", icon="mdi:contrast-box"),
    _sensor("highlight_clip", "Highlight clipping", unit="%", state_class="measurement",
            precision=2, category="diagnostic", icon="mdi:brightness-percent"),
    _sensor("noise", "Noise", state_class="measurement", precision=2,
            category="diagnostic", icon="mdi:grain"),
    _sensor("snr", "Signal to noise", state_class="measurement", precision=1,
            category="diagnostic", icon="mdi:signal"),
    _sensor("focus_sharpness", "Focus sharpness", state_class="measurement", precision=1,
            category="diagnostic", icon="mdi:image-filter-center-focus"),
    _sensor("battery", "Phone battery", unit="%", device_class="battery",
            state_class="measurement", category="diagnostic"),
    _sensor("battery_temperature", "Phone battery temperature", unit="°C",
            device_class="temperature", state_class="measurement", precision=1,
            category="diagnostic"),
    _sensor("archive_free_gb", "Archive free space", unit="GB", device_class="data_size",
            state_class="measurement", precision=1, category="diagnostic"),
    # Signed current is the one figure that says which way the battery is
    # actually going: the phone reports charging while still losing ground on
    # an underpowered port.
    _sensor("battery_current_ma", "Phone battery current", unit="mA",
            device_class="current", state_class="measurement", category="diagnostic"),
    _sensor("charger_watts", "Phone charger power", unit="W", device_class="power",
            state_class="measurement", category="diagnostic"),
    # No battery device class here: that one means state of charge, and this is
    # the cell's maximum against its design capacity.
    _sensor("battery_health_pct", "Phone battery health", unit="%",
            state_class="measurement", precision=1, category="diagnostic",
            icon="mdi:battery-heart-variant"),
    # How the last attempt at pressing Start streaming went. Options come from
    # stream_start rather than a second copy, for the same reason stream_state's
    # do: a value the code can produce but discovery does not list fails silently.
    _sensor("stream_start", "Stream start", device_class="enum",
            options=tuple(STREAM_START_STATES), category="diagnostic",
            icon="mdi:play-network"),
    # The step before it: whether the app itself had to be opened. Separate
    # because the two fail for different reasons -- no app at all, against an
    # app that is open but will not take the keys.
    _sensor("app_open", "App open", device_class="enum",
            options=tuple(APP_OPEN_STATES), category="diagnostic",
            icon="mdi:cellphone-play"),
    _sensor("stream_start_attempts", "Stream start attempts (1 h)", unit="attempts",
            state_class="measurement", category="diagnostic", icon="mdi:counter"),
    # Consecutive failures of calls needing the phone's developer services. Zero
    # normally; a number that stays up means app control is wedged, which is how
    # backgrounding stopped unnoticed for 24 hours on 2026-09-14.
    _sensor("dvt_failures", "Developer connection failures", unit="failures",
            state_class="measurement", category="diagnostic",
            icon="mdi:usb-flash-drive-outline"),
]
_SENSORS += [
    _sensor(f"lum_p{p}", f"Scene luminance {_ordinal(p)} percentile",
            state_class="measurement", precision=1, category="diagnostic",
            icon="mdi:brightness-6")
    for p in (1, 10, 50, 90, 99)
]
_SENSORS += [
    _sensor(f"wb_gain_{c}", f"White balance gain {c.upper()}",
            state_class="measurement", precision=3, category="diagnostic",
            icon="mdi:white-balance-auto")
    for c in "rgb"
]
_SENSORS += [
    _sensor(f"black_level_applied_{c}", f"Black level applied {c.upper()}",
            state_class="measurement", precision=2, category="diagnostic",
            icon="mdi:circle-opacity")
    for c in "rgb"
]
_SENSORS += [
    _sensor("holders", "Connection holders", category="diagnostic",
            icon="mdi:account-clock"),
    _sensor("live_fps", "Stacked feed rate", unit="fps", state_class="measurement",
            precision=1, category="diagnostic", icon="mdi:filmstrip"),
    _sensor("duty_1h", "Connected in the last hour", unit="%", state_class="measurement",
            precision=1, category="diagnostic", icon="mdi:percent-outline"),
    _sensor("tick_age", "Upstream tick age", unit="s", device_class="duration",
            state_class="measurement", precision=1, category="diagnostic",
            icon="mdi:heart-pulse"),
]

# stream_state answers "what is it doing" in one value, which is what a dashboard
# wants. These answer "which part is broken", which is what you want at 1am, and
# they are independent axes rather than a decomposition of that one enum: the
# cable and the WiFi can fail separately, and the app can be alive on a phone
# that is not currently sending anything.
_BINARY_SENSORS = [
    _binary("camera_online", "Camera online", "connectivity"),
    _binary("charging", "Phone charging", "battery_charging"),
    # Two independent transports to the same phone. Losing the cable costs
    # battery readings and app control; losing WiFi costs the picture.
    _binary("reachable_over_cable", "Reachable over cable", "connectivity"),
    _binary("reachable_over_wifi", "Reachable over WiFi", "connectivity"),
    # Read over the cable, so this is what separates a backgrounded app (alive,
    # socket suspended) from one iOS has killed. They look identical over WiFi.
    _binary("app_running", "App running", "running"),
    _binary("app_streaming", "App streaming"),
    # Who the frames are for. Both can be true at once.
    _binary("capturing_stills", "Capturing stills"),
    _binary("being_viewed", "Being viewed"),
    # Whether the power-saving policy is currently engaged. Distinct from the
    # `backgrounding` switch, which only says whether it is *allowed* to engage:
    # with a full battery the switch is on and this is off.
    _binary("backgrounding_active", "Backgrounding active"),
    # Whether a phone is attached to the keyboard arcturus emulates. Without it
    # the supervisor cannot press Start streaming, and the usual cause is
    # Bluetooth switched off on the phone.
    _binary("keyboard_connected", "Keyboard connected", "connectivity"),
]

# Declaration order is publication order, which is the order Home Assistant sees
# on a reconnect: cameras first so the device's main feature is registered before
# anything that could otherwise claim the device name.
ENTITIES = tuple(_CAMERAS + _NUMBERS + _SELECTS + _SWITCHES + _BUTTONS
                 + _SENSORS + _BINARY_SENSORS)


# The video and image the service serves over HTTP, which are not Home Assistant
# entities but are the other half of what a custom component has to be told about.
#
# `holds_phone` is what justifies the descriptor existing at all: /still.jpg is
# answered from memory and costs the phone nothing, which is what makes an
# on-demand upstream connection possible, while either stream holds that
# connection open for as long as a viewer stays attached.
MEDIA = (
    MediaSource(id="live", kind="stream", formats=("mjpeg",),
                path="/api/v1/stream/live", holds_phone=True),
    MediaSource(id="stacked", kind="stream", formats=("mjpeg",),
                path="/api/v1/stream/stacked", holds_phone=True),
    MediaSource(id="still", kind="snapshot", formats=("jpeg",),
                path="/api/v1/snapshot/still", holds_phone=False),
    # The phone's screen as the screenshot button last captured it. Like the
    # still it is served from memory, so asking for it costs the phone nothing;
    # taking a new one is the button's job. Before the first press the route
    # answers 503 rather than falling back to a camera frame, which would be an
    # answer to a different question.
    MediaSource(id="screenshot", kind="snapshot", formats=("jpeg",),
                path="/api/v1/snapshot/screenshot", holds_phone=False),
)
