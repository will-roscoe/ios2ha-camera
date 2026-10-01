"""Invariants of the entity registry.

`entities.ENTITIES` is now the single declaration behind the MQTT discovery
payloads, and will shortly be behind the JSON API and the generated docs page as
well. Everything here guards a way those could drift apart, or drift away from
the code that actually publishes the values.
"""

import pytest
from tests.test_supervisor import FakeMqtt, FakePhone, FakeUpstream, _fill, _sup

from ios2ha_camera.entities import ENTITIES, MEDIA
from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.ring_buffer import RingBuffer
from ios2ha_camera.settings import _CHOICES, BLACK_LEVEL_CHANNELS, CONTROLS, Settings

# A press, not a value: buttons are the one writable kind with no state at all,
# and the two cameras publish their JPEG and PNG to their own topics rather than
# a key under state/.
STATELESS_DOMAINS = {"button", "camera"}


def _capture_settings():
    s = Settings()
    # Stack from the buffer the test fills rather than waiting on the auto
    # depth law, and put a real black-level override in so publish_controls
    # emits the three per-channel numbers rather than skipping them.
    s.stacking_mode = "fixed"
    s.fixed_depth = 10
    s.black_level_override = (0.0, 0.0, 0.0)
    return s


def _published_key_sets():
    """The state keys each of the four publishers produces, gathered by running them.

    State is assembled in four disjoint places -- the health tick, the republish
    of every control on reconnect, the measurements taken around a capture, and
    the phone's own battery figures -- so the set of keys this service can
    publish is their union, and is derived here by driving the code rather than
    by restating it in a list that would need editing alongside every change.

    Both supervisor shapes are exercised for the health tick: without an upstream
    it publishes frame_rate unconditionally, and with one it adds the holders,
    duty and tick-age keys plus the booleans. Neither alone covers it.
    """
    buf = RingBuffer(5_000_000)
    _fill(buf, 20)
    mq = FakeMqtt()
    sup = _sup(_capture_settings(), buf, mq, phone=FakePhone())
    # Set by __main__ when the stacked feed exists, so health_state only reports
    # it when something is there to ask.
    sup.live_fps = lambda: 4.2

    sup.publish_controls()
    controls = set(mq.published)

    mq.states.clear()
    event = sup.produce_still(now_fps=12.0)
    assert "error" not in event, event      # a failed capture publishes nothing
    still = set(mq.published)

    health = set(sup.health_state())
    phone = set(sup.phone_state())

    with_upstream = _sup(_capture_settings(), buf, FakeMqtt(),
                         upstream=FakeUpstream(state="streaming"))
    with_upstream.live_fps = lambda: 4.2
    health |= set(with_upstream.health_state())

    return {"publish_controls": controls, "_produce_still": still,
            "health_state": health, "phone_state": phone}


def _published_keys():
    keys = set()
    for k in _published_key_sets().values():
        keys |= k
    return keys


def test_every_entity_has_a_publisher_and_every_published_value_has_an_entity():
    """The drift this whole refactor exists to prevent, asserted directly.

    A declared entity nothing publishes shows in Home Assistant as "unknown"
    forever, which reads as a fault rather than as "not fitted". A published key
    with no entity is a value nobody can see.
    """
    published = _published_keys()
    declared = {e.state_key for e in ENTITIES if e.state_key}
    assert declared - published == set(), "declared but never published"
    assert published - declared == set(), "published but not declared"


def test_frame_rate_is_the_only_value_two_code_paths_publish():
    """Known and deliberately left alone by the extraction that added this file.

    supervisor.health_state and supervisor._produce_still both write frame_rate
    and compute it differently -- one over the buffer since the session started,
    the other from the capture's own measured rate. Unifying them is a behaviour
    change, not a move. This pins the overlap at exactly one key so a second one
    has to be noticed and argued for rather than arriving unseen.
    """
    sets = _published_key_sets()
    overlapping = {k for k in _published_keys()
                   if sum(k in s for s in sets.values()) > 1}
    assert overlapping == {"frame_rate"}


def test_object_ids_are_unique_within_a_domain():
    """The discovery topic and the unique_id are both (domain, object_id), so a
    duplicate is not a second entity -- it is one entity overwriting another.
    `screenshot` exists twice on purpose, as a button and as a camera."""
    seen = set()
    for e in ENTITIES:
        key = (e.domain, e.object_id)
        assert key not in seen, f"{e.domain}.{e.object_id} is declared twice"
        seen.add(key)


def test_enum_entities_declare_their_options():
    """Home Assistant silently drops a state not in the declared options: the
    entity just goes unavailable, with nothing in any log to say why."""
    enums = [e for e in ENTITIES if e.device_class == "enum" or e.domain == "select"]
    assert enums, "no enum entities found"
    for e in enums:
        assert e.options, f"{e.object_id} is an enum with no options"
        assert len(set(e.options)) == len(e.options), f"{e.object_id} repeats an option"
        assert all(isinstance(o, str) and o for o in e.options), e.object_id
    # Nothing else may carry options: they mean "these are the only valid states"
    # and on a non-enum entity Home Assistant ignores them.
    for e in ENTITIES:
        if e.options:
            assert e in enums, f"{e.object_id} has options but is not an enum"


def test_a_selects_options_are_exactly_what_settings_will_accept():
    """The vocabulary is declared twice -- here and in settings._CHOICES -- and
    until now nothing compared the two. An option Home Assistant offers that
    Settings rejects is a control a person can pick and watch snap back; a value
    Settings accepts that no option offers can only ever arrive from a script."""
    for e in ENTITIES:
        if e.domain != "select":
            continue
        assert tuple(e.options) == _CHOICES[e.object_id], e.object_id
        s = Settings()
        for option in e.options:
            assert s.apply_control(e.object_id, option) is True, (e.object_id, option)
        with pytest.raises(ValueError):
            s.apply_control(e.object_id, "not-an-option")


def test_every_value_control_names_a_real_setting_and_every_setting_has_one():
    """Stronger than the stem match in test_mqtt_io, which only checked that some
    command topic looked like each control's name.

    black_level_override is the one control with no entity of its own: it is a
    3-tuple, exposed as the three black_level_N numbers that write into it.
    """
    declared = {e.object_id for e in ENTITIES
                if e.writable and e.domain not in STATELESS_DOMAINS}
    settable = CONTROLS - {"black_level_override"} | set(BLACK_LEVEL_CHANNELS)
    assert declared == settable


def test_buttons_carry_no_state_and_everything_else_does():
    for e in ENTITIES:
        if e.domain in STATELESS_DOMAINS:
            assert e.state_key is None, f"{e.object_id} is stateless but names a state key"
        else:
            assert e.state_key == e.object_id, e.object_id
    for e in ENTITIES:
        assert (e.topic is not None) == (e.domain == "camera"), e.object_id


def test_writable_is_exactly_the_set_that_gets_a_command_topic():
    """`writable` is what the API task will read to decide what accepts a write,
    so it has to mean the same thing there as it does in the discovery payload."""
    payloads = dict(discovery_payloads(Settings()))
    for e in ENTITIES:
        cfg = payloads[f"homeassistant/{e.domain}/ios2ha/{e.object_id}/config"]
        assert e.writable == ("command_topic" in cfg), e.object_id


def test_every_declared_button_has_a_dispatch_branch():
    """A button with no branch in handle_control falls through to _apply_setting,
    which finds no such setting and logs "ignoring unknown control" -- so the
    button appears in Home Assistant, is pressable, and does nothing at all.
    Nothing caught that before this test."""
    sup = _sup(starter=object(), rebooter=object(), flat=object(), onvif=object(),
               colour=object())
    submitted = []
    fell_through = []
    # Recorded rather than run: this is about which branch the name reaches, and
    # the actions themselves want a real phone, keyboard and ONVIF camera.
    sup._submit = lambda name, fn: submitted.append(name)
    sup._apply_setting = lambda name, value: fell_through.append(name)

    for e in ENTITIES:
        if e.domain != "button":
            continue
        sup.capture_requested.clear()
        submitted.clear()
        sup.handle_control(e.object_id, None)
        assert not fell_through, f"{e.object_id} has no dispatch branch"
        assert submitted == [e.object_id] or sup.capture_requested.is_set(), \
            f"{e.object_id} was dispatched but did nothing"


def test_the_registry_renders_every_entity_and_nothing_else():
    payloads = discovery_payloads(Settings())
    assert len(payloads) == len(ENTITIES)
    assert {t for t, _ in payloads} == {
        f"homeassistant/{e.domain}/ios2ha/{e.object_id}/config" for e in ENTITIES}


def test_every_entity_has_a_readable_name_except_the_main_feature():
    for e in ENTITIES:
        if e.name is None:
            # A null name inherits the device's, which marks the device's main
            # feature. Exactly one entity may do that.
            assert (e.domain, e.object_id) == ("camera", "still")
            continue
        assert e.name != e.object_id, f"{e.object_id} has a raw object id as its name"
        assert e.name[0].isupper(), f"{e.object_id} is not sentence case: {e.name!r}"


def test_media_sources_are_distinct_and_say_what_they_cost():
    """`holds_phone` is the field the whole descriptor exists for: a snapshot is
    answered from memory, while a stream holds the phone's connection open for as
    long as a viewer stays attached, which is what on-demand upstream turns on."""
    assert len(MEDIA) == 5
    assert len({m.id for m in MEDIA}) == len(MEDIA)
    assert len({m.path for m in MEDIA}) == len(MEDIA)
    for m in MEDIA:
        assert m.kind in ("stream", "snapshot", "video")
        assert m.path.startswith("/api/v1/")
        assert m.formats
    assert {m.id for m in MEDIA if m.holds_phone} == {"live", "stacked"}
    assert {m.id for m in MEDIA if m.kind == "snapshot"} == {"still", "screenshot"}
    assert {m.id for m in MEDIA if m.kind == "video"} == {"timelapse"}


def test_every_entity_says_what_it_does():
    """The entity reference in the docs is generated from these, and until 2.13.0
    it listed ids and units with nothing saying what any of them did."""
    from ios2ha_camera.entities import _DESCRIPTIONS
    undescribed = [(e.domain, e.object_id) for e in ENTITIES if len(e.description) < 20]
    assert undescribed == []
    stale = set(_DESCRIPTIONS) - {(e.domain, e.object_id) for e in ENTITIES}
    assert stale == set(), "a description for an entity that no longer exists"
