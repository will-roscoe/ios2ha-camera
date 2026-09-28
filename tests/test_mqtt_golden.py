"""The MQTT discovery surface, pinned.

Home Assistant entities are the released public interface of this service: an
automation or dashboard names an entity_id, and a unique_id is what stops a
re-discovery creating a second copy of an entity rather than updating the one
that exists. Moving where the declaration lives must not change what is
published, so this file records what the code publishes and fails on any
difference -- including the ones a hand-written assertion would miss, like a key
quietly gaining or losing a value.

There are two golden files and two comparisons, because they catch different
things and fail differently:

- `discovery_payloads.json` is sorted and pretty-printed, and is compared as
  parsed dictionaries. It cannot see key order, but when it fails pytest's diff
  names the entity and the key that moved, which is what you want to read.
- `discovery_payloads.wire.txt` is the exact string each payload is published as
  (`MqttIO._on_connect` sends `json.dumps(cfg)`), one line per entity, in
  publication order. It is compared as text, so it also catches a key order or
  an entity order that changed -- neither of which Home Assistant minds, but
  both of which mean the payload on the wire is not the one that was pinned, and
  this suite is the only thing standing behind that claim.

Both have the `sw_version` stripped: it changes on every release, and a golden
test that had to be regenerated for each one would be regenerated without being
read. Nothing else is normalised away.

To regenerate both after a *deliberate* surface change (and only then -- read the
diff first, it is the public interface):

    ~/.venvs/ios2ha-camera/bin/python -c \
        "from tests.test_mqtt_golden import write_golden; write_golden()"
"""

import json
from pathlib import Path

from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.settings import Settings

DATA = Path(__file__).parent / "data"
GOLDEN = DATA / "discovery_payloads.json"
GOLDEN_WIRE = DATA / "discovery_payloads.wire.txt"


def _without_version(cfg):
    """A copy with `device.sw_version` removed, key order otherwise untouched.

    Rebuilt rather than popped in place so the function cannot mutate the caller's
    payload, and by comprehension rather than json round-trip so the ordering this
    module exists to check is preserved rather than re-derived.
    """
    return {k: ({dk: dv for dk, dv in v.items() if dk != "sw_version"}
                if k == "device" and isinstance(v, dict) else v)
            for k, v in cfg.items()}


def _normalised():
    """Discovery payloads with the version stripped, keyed by topic."""
    return {topic: _without_version(cfg) for topic, cfg in discovery_payloads(Settings())}


def _wire():
    """Exactly what gets published, one `<topic> <payload>` line per entity.

    A topic never contains a space, so the first space is an unambiguous split
    and the file stays readable in a diff.
    """
    return "".join(f"{topic} {json.dumps(_without_version(cfg))}\n"
                   for topic, cfg in discovery_payloads(Settings()))


def write_golden():
    """Regenerate both golden files. Never called by the tests; see the module docstring."""
    DATA.mkdir(exist_ok=True)
    GOLDEN.write_text(json.dumps(_normalised(), indent=2, sort_keys=True))
    GOLDEN_WIRE.write_text(_wire())


def test_discovery_payloads_are_unchanged():
    """No entity, key or value has changed.

    Parsed, so it is blind to ordering -- that is what the wire test below is for
    -- but a failure here points at the one entity and key that differ, where the
    wire test can only show two long strings.
    """
    assert _normalised() == json.loads(GOLDEN.read_text())


def test_discovery_payloads_are_byte_identical_on_the_wire():
    """The published bytes are unchanged, including key order and entity order.

    `MqttIO._on_connect` publishes `json.dumps(cfg)`, so key order really is part
    of what goes out; comparing parsed dictionaries would accept a builder that
    emitted the same keys in a different order and quietly break the "not one
    byte differs" promise this suite is here to keep. Compared as text, in
    publication order, with only `sw_version` removed.
    """
    assert _wire() == GOLDEN_WIRE.read_text()


def test_the_golden_files_cover_every_published_entity():
    """A golden file that had silently lost entries would pass the comparisons
    above while asserting nothing about the ones it dropped."""
    assert len(discovery_payloads(Settings())) == 104
    assert len(json.loads(GOLDEN.read_text())) == 104
    assert len(GOLDEN_WIRE.read_text().splitlines()) == 104
