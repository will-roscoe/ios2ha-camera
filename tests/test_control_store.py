"""Control values survive a restart.

Measured 2026-09-23..28: every phone reboot restarts the service through the
usbmuxd hook, nine times in that week, and each restart put every control back
to its default. Autofocus switched off at 22:22 on the 24th was back on at 14:20
on the 25th; night_key, chroma_radius and clean_factor went the same way.
"""
import json
import os

from ios2ha_camera.control_store import ControlStore
from ios2ha_camera.settings import CONTROLS, Settings


def _changed():
    s = Settings()
    s.apply_control("interval_s", 300)
    s.apply_control("night_key", 0.06)
    s.apply_control("chroma_radius", 1.5)
    s.apply_control("stacking_mode", "fixed")
    s.apply_control("autofocus", False)
    s.apply_control("focus_position", 0.8)
    s.apply_control("backgrounding", False)
    s.apply_control("black_level_1", 2.5)
    return s


def test_every_control_round_trips(tmp_path):
    path = tmp_path / "controls.json"
    saved = _changed()
    ControlStore(str(path)).save(saved)

    fresh = Settings()
    ControlStore(str(path)).load(fresh)
    for name in CONTROLS:
        assert getattr(fresh, name) == getattr(saved, name), name
    assert fresh.black_level == saved.black_level


def test_the_black_level_override_comes_back_in_force(tmp_path):
    """The renderer reads black_level_override, not the switch: restoring the
    switch and the numbers without re-deriving it would restore a setting that
    does nothing."""
    path = tmp_path / "controls.json"
    ControlStore(str(path)).save(_changed())
    fresh = Settings()
    ControlStore(str(path)).load(fresh)
    assert fresh.black_level_override_on is True
    assert fresh.black_level_override == (0.0, 2.5, 0.0)


def test_a_missing_file_changes_nothing(tmp_path):
    s = Settings()
    assert ControlStore(str(tmp_path / "absent.json")).load(s) == []
    assert s == Settings()


def test_a_corrupt_file_keeps_the_defaults_and_says_so(tmp_path, caplog):
    path = tmp_path / "controls.json"
    path.write_text("{not json")
    s = Settings()
    with caplog.at_level("WARNING"):
        assert ControlStore(str(path)).load(s) == []
    assert s == Settings()
    assert any("controls" in r.getMessage() for r in caplog.records)


def test_one_bad_value_costs_only_itself(tmp_path, caplog):
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"stacking_mode": "sideways", "night_key": 0.05,
                                "autofocus": "banana", "interval_s": 120}))
    s = Settings()
    with caplog.at_level("WARNING"):
        restored = ControlStore(str(path)).load(s)
    assert sorted(restored) == ["interval_s", "night_key"]
    assert s.stacking_mode == "auto" and s.autofocus is True
    assert s.night_key == 0.05 and s.interval_s == 120
    assert any("stacking_mode" in r.getMessage() for r in caplog.records)


def test_names_that_are_not_controls_are_ignored(tmp_path):
    """The file is ours, but nothing in it may reach a deployment field: a
    phone address or password comes from the environment and nowhere else."""
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"phone_host": "10.0.0.99", "phone_password": "x",
                                "no_such_control": 1, "night_key": 0.07}))
    s = Settings()
    assert ControlStore(str(path)).load(s) == ["night_key"]
    assert s.phone_host == Settings().phone_host
    assert s.phone_password == ""


def test_only_controls_are_written(tmp_path):
    s = _changed()
    s.phone_password = "secret"
    path = tmp_path / "controls.json"
    ControlStore(str(path)).save(s)
    data = json.loads(path.read_text())
    assert "phone_password" not in data and "secret" not in path.read_text()
    assert set(data) == (CONTROLS - {"black_level_override"}) | {"black_level"}


def test_a_save_replaces_the_file_whole(tmp_path):
    """Written beside and renamed over, so a crash mid-write leaves the old
    values rather than half a file."""
    path = tmp_path / "controls.json"
    store = ControlStore(str(path))
    store.save(Settings())
    store.save(_changed())
    assert json.loads(path.read_text())["night_key"] == 0.06
    assert os.listdir(tmp_path) == ["controls.json"]


def test_a_save_that_cannot_write_logs_and_carries_on(tmp_path, caplog):
    store = ControlStore(str(tmp_path / "no-such-dir" / "controls.json"))
    with caplog.at_level("WARNING"):
        assert store.save(Settings()) is False
    assert any("could not save" in r.getMessage() for r in caplog.records)
