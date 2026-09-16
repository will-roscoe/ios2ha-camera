"""The MQTT discovery surface, pinned byte for byte.

Home Assistant entities are the released public interface of this service: an
automation or dashboard names an entity_id, and a unique_id is what stops a
re-discovery creating a second copy of an entity rather than updating the one
that exists. Moving where the declaration lives must not change one byte of what
is published, so this file records what today's code publishes and fails on any
difference at all -- including the ones a hand-written assertion would miss,
like a key quietly gaining or losing a value.
"""

import json
from pathlib import Path

from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.settings import Settings

GOLDEN = Path(__file__).parent / "data" / "discovery_payloads.json"


def _normalised():
    """Discovery payloads with the version stripped, which changes every release."""
    out = {}
    for topic, cfg in discovery_payloads(Settings()):
        cfg = json.loads(json.dumps(cfg))          # deep copy
        cfg.get("device", {}).pop("sw_version", None)
        out[topic] = cfg
    return out


def test_discovery_payloads_are_unchanged():
    """The MQTT surface is the existing public interface. This refactor moves where the
    declaration lives; it must not change one byte of what is published."""
    assert _normalised() == json.loads(GOLDEN.read_text())


def test_the_golden_file_covers_every_published_entity():
    """A golden file that has silently lost entries would pass the comparison above
    while asserting nothing about the ones it dropped."""
    assert len(json.loads(GOLDEN.read_text())) == len(discovery_payloads(Settings())) == 86
