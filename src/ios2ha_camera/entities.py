"""Every entity this service publishes, declared once.

These records are the whole declaration. `mqtt_io` is one renderer over them,
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

from dataclasses import dataclass, field, replace

from . import version
from .app_open import APP_OPEN_STATES
from .emergency import EMERGENCY_STATES
from .reboot import REBOOT_STATES
from .states import STREAM_STATES
from .stream_start import STREAM_START_STATES


def device_info():
    """The device every entity hangs off, declared once for both renderers.

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
    # What the entity shows or controls, in plain words, for the generated entity
    # reference in the docs. Filled from _DESCRIPTIONS below rather than passed
    # to each helper, so the declarations above stay readable; a test holds every
    # entity to having one. Not served by MQTT or the API: it is documentation.
    description: str = ""


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
    _number("emergency_below_pct", "Emergency keyboard below", lo=0, hi=100, step=1,
            unit="%", icon="mdi:battery-alert"),
    _number("black_level_0", "Black level override R", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("black_level_1", "Black level override G", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("black_level_2", "Black level override B", lo=0, hi=32, step=0.1,
            icon="mdi:circle-opacity"),
    _number("live_max_window_s", "Stacked feed window", lo=0.5, hi=15, step=0.5, unit="s",
            device_class="duration", icon="mdi:motion-outline"),
    _number("focus_position", "Focus position", lo=0, hi=1, step=0.01,
            icon="mdi:image-filter-center-focus-weak"),
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
    _switch("auto_reboot", "Auto reboot wedged phone", "mdi:restart-alert"),
    _switch("emergency_keyboard", "Emergency keyboard backgrounding",
            "mdi:keyboard-outline"),
    _switch("black_level_override_on", "Black level override",
            "mdi:circle-opacity"),
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
    # Reboots the phone now, skipping the wedge test, the budget and the latch:
    # a person can see what this service cannot. The reachability guard holds,
    # because an unreachable phone has nothing to send the request over.
    _button("reboot_phone", "Reboot phone", device_class="restart",
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
    # Changes once per still and at no other time, which makes it the trigger a
    # timelapse automation wants. The per-still MQTT event did that job, but it
    # reaches nobody with MQTT off, and the JSON API carries no events at all.
    _sensor("last_still", "Last still", device_class="timestamp", icon="mdi:camera-timer"),
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
    _sensor("emergency_background", "Emergency backgrounding", device_class="enum",
            options=tuple(EMERGENCY_STATES), category="diagnostic",
            icon="mdi:keyboard-off-outline"),
    _sensor("phone_reboot", "Phone reboot", device_class="enum",
            options=tuple(REBOOT_STATES), category="diagnostic",
            icon="mdi:restart"),
    _sensor("phone_reboots", "Phone reboots (24 h)", unit="reboots",
            state_class="measurement", category="diagnostic", icon="mdi:counter"),
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
    _binary("backgrounded_by_keyboard", "Backgrounded by keyboard"),
    # Whether a phone is attached to the keyboard arcturus emulates. Without it
    # the supervisor cannot press Start streaming, and the usual cause is
    # Bluetooth switched off on the phone.
    _binary("keyboard_connected", "Keyboard connected", "connectivity"),
]

# What each entity shows or controls, keyed by (domain, object_id) because
# `screenshot` is both a camera and a button. Written for someone looking at the
# entity in Home Assistant and wondering what it is for.
_DESCRIPTIONS = {
    # -- cameras
    ("camera", "still"):
        "The latest stacked still, made every capture interval: many frames averaged into one, "
        "then brightened for the dark. Served from memory, so viewing it never wakes the phone. "
        "This is the device's main picture.",
    ("camera", "screenshot"):
        "The phone's screen, as the Phone screenshot button last captured it. Empty until the "
        "button has been pressed since the service started.",
    # -- numbers
    ("number", "interval_s"):
        "How often a still is made. Each one wakes the phone for as long as it takes to gather "
        "its frames, so a shorter interval costs battery.",
    ("number", "background_below_pct"):
        "Battery level below which the camera app is sent to the background between captures, "
        "so the phone can charge. The lower end of a band with Resume above.",
    ("number", "resume_above_pct"):
        "Battery level above which the app is left in the foreground again. Must be above "
        "Background below, or backgrounding switches itself off and logs why.",
    ("number", "fixed_depth"):
        "Frames per still when Stacking mode is fixed.",
    ("number", "target_level"):
        "How bright the dark part of the scene should come out, 0-255. The service stacks "
        "enough frames to reach it without the noise showing; frames needed rise with the "
        "square of this, so doubling it quadruples capture time.",
    ("number", "subject_percentile"):
        "Which part of the picture counts as the subject for Target subject level: the darkest "
        "this many per cent of pixels. Lower stacks harder for the shadows.",
    ("number", "clean_factor"):
        "How clean a still must be, as a multiplier on frames stacked. Raise for less grain at "
        "the cost of longer captures.",
    ("number", "max_depth"):
        "The most frames any still may stack, whatever the light asks for.",
    ("number", "window_max_s"):
        "The longest a still may gather frames over. Keeps moving things from smearing and caps "
        "how long each capture keeps the phone awake.",
    ("number", "ramp_tau_s"):
        "How gradually frames per still follow the light. At 900 s, dusk raises depth over about "
        "fifteen minutes rather than all at once.",
    ("number", "night_key"):
        "How bright the night picture is rendered. Lower is darker and moodier and shows less "
        "grain; higher lifts the shadows. Free to change: it only affects rendering.",
    ("number", "stretch_strength"):
        "How hard the inspection render stretches shadows. Only used when Render intent is "
        "inspection.",
    ("number", "chroma_radius"):
        "How much colour speckle is smoothed away, in pixels. Blurs colour only, not detail, but "
        "large values bleed colour across edges. Scaled down automatically in daylight.",
    ("number", "denoise_strength"):
        "How hard grain is smoothed after stacking. Higher is cleaner but softer, and paints flat "
        "areas into blotches. Scaled down automatically in daylight.",
    ("number", "emergency_below_pct"):
        "Battery level below which the Bluetooth keyboard may press Home to background the app, "
        "when the cable cannot. Emergency use only.",
    ("number", "black_level_0"):
        "Fixed red black level, used while Black level override is on. Writing it switches the "
        "override on.",
    ("number", "black_level_1"):
        "Fixed green black level, used while Black level override is on. Writing it switches the "
        "override on.",
    ("number", "black_level_2"):
        "Fixed blue black level, used while Black level override is on. Writing it switches the "
        "override on.",
    ("number", "live_max_window_s"):
        "How many seconds of frames the stacked live feed blends together. Longer is cleaner but "
        "smears movement.",
    ("number", "focus_position"):
        "Lens position, 0 (near) to 1 (far end of travel, which is past infinity). With "
        "autofocus off the lens is held here, and put back at every capture; with it on, this "
        "shows where autofocus put the lens. Writing it switches autofocus off.",
    # -- selects
    ("select", "stacking_mode"):
        "auto stacks as many frames as the light needs; fixed always stacks Fixed depth; off "
        "uses a single frame.",
    ("select", "white_balance"):
        "as-shot keeps the phone's colour; brightest-patch neutralises the brightest area; "
        "grey-world assumes the scene averages to grey, which a mostly one-colour view defeats.",
    ("select", "render_intent"):
        "natural renders a night picture that looks like a photograph; inspection stretches the "
        "shadows to read detail, and looks processed.",
    # -- switches
    ("switch", "stacking_allowed"):
        "Off forces single-frame stills whatever Stacking mode says.",
    ("switch", "autofocus"):
        "On: the phone focuses itself. Off: the lens is held at Focus position. Night autofocus, "
        "especially through glass, can settle well off the scene.",
    ("switch", "on_demand"):
        "On: the phone is connected only while capturing or while someone views a live feed. "
        "Off: connected all the time, which drains the battery.",
    ("switch", "backgrounding"):
        "Allows the app to be backgrounded between captures when the battery falls into the "
        "Background below / Resume above band. Off brings it back to the foreground.",
    ("switch", "auto_start"):
        "Allows the service to press Start streaming in the app itself, over the Bluetooth "
        "keyboard, and to reopen the app after a phone restart.",
    ("switch", "auto_reboot"):
        "Allows the service to reboot the phone when its developer services have been wedged "
        "for fifteen minutes. Turn off while investigating a wedge by hand.",
    ("switch", "emergency_keyboard"):
        "Allows the Bluetooth keyboard to background the app when the cable cannot and the "
        "battery is below Emergency keyboard below.",
    ("switch", "black_level_override_on"):
        "On: use the three fixed Black level numbers. Off: measure the black level from each "
        "still, which is almost always better.",
    # -- buttons
    ("button", "capture_now"):
        "Makes a still now, without moving the schedule.",
    ("button", "screenshot"):
        "Takes a screenshot of the phone's screen, shown on the Screenshot camera. Useful for "
        "seeing what the app is showing.",
    ("button", "relaunch_app"):
        "Relaunches the camera app over the cable. Streaming comes back off, so use it only for "
        "a dead app.",
    ("button", "prepare_phone"):
        "Runs the phone set-up checks over the cable: connection, Developer Mode, developer "
        "image, app launch. Reported as an event.",
    ("button", "start_streaming"):
        "Presses Start streaming in the app now, over the Bluetooth keyboard, ignoring the Auto "
        "start switch and its rate limit. Still refuses unless the app is open and idle.",
    ("button", "reboot_phone"):
        "Reboots the phone now, ignoring the Auto reboot switch and its limits. The camera is "
        "dark for about five minutes.",
    # -- sensors
    ("sensor", "stream_state"):
        "What the camera is doing: unavailable (nothing answers), backgrounded (the app is "
        "asleep), streaming_off (app open, Start streaming not pressed), stalled (the app's "
        "picture has frozen), ready (idle and healthy), or streaming / capturing / in_use (frames "
        "flowing, for nobody in particular / a still / a viewer).",
    ("sensor", "cable_state"):
        "The USB cable side: ok, no_device (unplugged, which is ordinary), or usbmuxd_down (the "
        "host's USB daemon is unreachable: battery, app control and reboots are all dead).",
    ("sensor", "subject_level"):
        "How bright the subject was in the last still before rendering, 0-255. What the frames "
        "per still are chosen from.",
    ("sensor", "depth_used"):
        "Frames stacked into the last still.",
    ("sensor", "motion"):
        "How much changed between the last two frames looked at. Near the noise level on a still "
        "scene; higher with movement.",
    ("sensor", "frame_rate"):
        "Frames per second arriving from the phone while connected.",
    ("sensor", "last_frame"):
        "When the newest frame arrived from the phone.",
    ("sensor", "last_still"):
        "When the capture for the last still started. Changes once per still, when it is "
        "published -- at night minutes later -- so it is the trigger for a timelapse automation.",
    ("sensor", "depth_required"):
        "Frames the light asked for before the limits applied. Far above Depth used means the "
        "limits, not the light, are setting depth.",
    ("sensor", "capture_duration"):
        "How long the last still took from start to published, including stacking and "
        "rendering. Above the capture interval, stills are being missed.",
    ("sensor", "colour_temperature"):
        "Estimated colour temperature of the brightest part of the last still, in kelvin.",
    ("sensor", "effective_exposure_required"):
        "The exposure the light asked for, as seconds of frames: Depth required over the frame "
        "rate.",
    ("sensor", "effective_exposure_achieved"):
        "The exposure the last still actually got, as seconds of frames: Depth used over the "
        "frame rate.",
    ("sensor", "lum_mean"):
        "Average brightness of the last still before rendering, 0-255 above black.",
    ("sensor", "dynamic_range"):
        "Ratio of the brightest to the darkest parts of the last still, before black-level "
        "subtraction.",
    ("sensor", "highlight_clip"):
        "Share of the last still that is blown out to white in any colour.",
    ("sensor", "noise"):
        "Grain left in the darkest tenth of the finished still, after all smoothing.",
    ("sensor", "snr"):
        "Signal to noise in the darkest tenth of the finished still. Higher is cleaner.",
    ("sensor", "focus_sharpness"):
        "Edge detail in the last stacked still. Compare it at similar light only: at night it "
        "rises with grain as well as with focus.",
    ("sensor", "battery"):
        "The phone's battery level, read over the cable every five minutes.",
    ("sensor", "battery_temperature"):
        "The phone's battery temperature.",
    ("sensor", "archive_free_gb"):
        "Free space where stills are archived. Archiving stops below 2 GB.",
    ("sensor", "battery_current_ma"):
        "Current into the battery; negative means it is losing charge even while plugged in. "
        "The honest charging figure on a weak supply.",
    ("sensor", "charger_watts"):
        "What the phone reports its power supply can give.",
    ("sensor", "battery_health_pct"):
        "The battery's maximum capacity against when new.",
    ("sensor", "stream_start"):
        "How the last attempt to press Start streaming went, from idle through started to the "
        "reasons it could not (see OPERATIONS.md).",
    ("sensor", "app_open"):
        "How the last attempt to open or bring forward the camera app went.",
    ("sensor", "emergency_background"):
        "How the last emergency keyboard backgrounding went. Anything but idle means the cable "
        "could not background the app and the battery was low.",
    ("sensor", "phone_reboot"):
        "How the last automatic or manual phone reboot went: rebooting, recovered, still_wedged, "
        "given_up and so on (see OPERATIONS.md).",
    ("sensor", "phone_reboots"):
        "Phone reboots in the last 24 hours, against the limit of three a day.",
    ("sensor", "stream_start_attempts"):
        "Start streaming attempts in the last hour, against the rate limit.",
    ("sensor", "dvt_failures"):
        "Consecutive failures of the phone's developer services, which backgrounding and app "
        "control need. Zero normally; a number that stays up is a wedge.",
    ("sensor", "lum_p1"): "Brightness of the darkest 1% of the last still, 0-255 above black.",
    ("sensor", "lum_p10"): "Brightness of the darkest 10% of the last still, 0-255 above black.",
    ("sensor", "lum_p50"): "Median brightness of the last still, 0-255 above black.",
    ("sensor", "lum_p90"): "Brightness below which 90% of the last still sits, 0-255 above black.",
    ("sensor", "lum_p99"): "Brightness below which 99% of the last still sits, 0-255 above black.",
    ("sensor", "wb_gain_r"): "Red gain the white balance applied to the last still. 1 is none.",
    ("sensor", "wb_gain_g"): "Green gain the white balance applied to the last still. 1 is none.",
    ("sensor", "wb_gain_b"): "Blue gain the white balance applied to the last still. 1 is none.",
    ("sensor", "black_level_applied_r"):
        "Red black level subtracted from the last still: measured, or the override while it is on.",
    ("sensor", "black_level_applied_g"):
        "Green black level subtracted from the last still: measured, or the override while it is "
        "on.",
    ("sensor", "black_level_applied_b"):
        "Blue black level subtracted from the last still: measured, or the override while it is "
        "on.",
    ("sensor", "holders"):
        "Who is keeping the phone connected right now: capture, viewer, either with (linger) "
        "just after they finish, or none.",
    ("sensor", "live_fps"):
        "Frames per second the stacked live feed is producing, while someone watches it.",
    ("sensor", "duty_1h"):
        "Share of the last hour the phone was connected. About 10-12% from captures alone; much "
        "more means something is holding it.",
    ("sensor", "tick_age"):
        "Seconds since the connection manager last checked in. Above 180 it has stalled and "
        "every other reading is stale.",
    # -- binary sensors
    ("binary_sensor", "camera_online"):
        "Whether the camera is working: frames recently, or idle and healthy, or deliberately "
        "backgrounded to charge. What the offline alert watches.",
    ("binary_sensor", "charging"):
        "Whether the phone says it is charging. It can say so while still losing charge; see "
        "Phone battery current.",
    ("binary_sensor", "reachable_over_cable"):
        "Whether the USB cable connection to the phone works.",
    ("binary_sensor", "reachable_over_wifi"):
        "Whether the phone answers on WiFi, even if the app is asleep.",
    ("binary_sensor", "app_running"):
        "Whether the camera app's process is alive, read over the cable. Separates an app that "
        "is asleep from one iOS has killed.",
    ("binary_sensor", "app_streaming"):
        "Whether the app is open and willing to send frames.",
    ("binary_sensor", "capturing_stills"):
        "Whether a still is being captured right now.",
    ("binary_sensor", "being_viewed"):
        "Whether someone is watching a live feed right now, which keeps the phone connected.",
    ("binary_sensor", "backgrounding_active"):
        "Whether the battery saver is engaged, backgrounding the app between captures. The "
        "Backgrounding switch only allows it.",
    ("binary_sensor", "backgrounded_by_keyboard"):
        "Whether the app was last backgrounded by the emergency keyboard rather than the cable.",
    ("binary_sensor", "keyboard_connected"):
        "Whether the phone is connected to the Bluetooth keyboard the service emulates. Needed "
        "to press Start streaming; usually off because Bluetooth is off on the phone.",
}

# Declaration order is publication order, which is the order Home Assistant sees
# on a reconnect: cameras first so the device's main feature is registered before
# anything that could otherwise claim the device name.
ENTITIES = tuple(replace(e, description=_DESCRIPTIONS.get((e.domain, e.object_id), ""))
                 for e in _CAMERAS + _NUMBERS + _SELECTS + _SWITCHES + _BUTTONS
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
