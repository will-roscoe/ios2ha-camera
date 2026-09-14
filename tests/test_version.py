import re

from ios2ha_camera import __version__, version
from ios2ha_camera.mqtt_io import discovery_payloads
from ios2ha_camera.settings import Settings


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.VERSION), version.VERSION


def test_the_package_exports_one_version_not_a_second_copy():
    """pyproject.toml is the single source; a duplicate literal in the source
    would drift the moment one of them was bumped."""
    assert __version__ == version.VERSION


def test_the_newest_changelog_entry_is_the_packaged_version():
    """2.3.0 was tagged and deployed with pyproject.toml still at 2.2.0, so the
    device page read "2.2.0 (v2.3.0)". Read from the file rather than the
    installed metadata, which an editable install leaves stale until reinstalled."""
    import pathlib
    import tomllib

    root = pathlib.Path(__file__).resolve().parents[1]
    packaged = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    newest = re.search(r"^## (\d+\.\d+\.\d+)", (root / "CHANGELOG.md").read_text(), re.M)
    assert newest and newest.group(1) == packaged, (newest and newest.group(1), packaged)


def test_provenance_falls_back_to_source_outside_a_built_image():
    # The build args are only set by the Dockerfile, so a source run says so
    # rather than claiming a commit it cannot know.
    assert version.COMMIT and version.BUILT
    assert version.full().startswith(version.VERSION)


def test_full_carries_the_commit_as_well_as_the_number(monkeypatch):
    """A semantic version alone cannot distinguish a clean release from a
    release plus uncommitted edits, and the deployment is an rsync."""
    monkeypatch.setattr(version, "COMMIT", "v1.0.0-3-gabc1234-dirty")
    assert version.full() == f"{version.VERSION} (v1.0.0-3-gabc1234-dirty)"


def test_home_assistant_is_told_the_running_version():
    """So "what is deployed?" is answerable from the device page, without
    shell access to the host."""
    for _topic, cfg in discovery_payloads(Settings()):
        device = cfg["device"]
        assert device["sw_version"].startswith(version.VERSION), cfg
        assert device["identifiers"] == ["ios2ha_camera"]


def test_every_entity_reports_the_same_device():
    """One device, not one per entity: a mismatch would scatter them across
    the Home Assistant UI."""
    import json

    devices = {json.dumps(cfg["device"], sort_keys=True)
               for _t, cfg in discovery_payloads(Settings())}
    assert len(devices) == 1, devices
