"""deploy.sh takes the host and paths from the environment or an untracked deploy.env."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy.sh"
VARS = ("IOS2HA_HOST", "IOS2HA_SRC", "IOS2HA_STACK")


@pytest.fixture
def copy(tmp_path):
    """deploy.sh alone in a directory, with a fake ssh first on PATH that records its arguments."""
    work = tmp_path / "repo"
    work.mkdir()
    shutil.copy(SCRIPT, work / "deploy.sh")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ssh").write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path}/ssh-calls\necho "ios2ha-camera 2.17.1 (v2.17.1)"\n'
    )
    (fake / "ssh").chmod(0o755)
    env = {"PATH": f"{fake}:{os.environ['PATH']}"}
    return work, env, tmp_path / "ssh-calls"


def run(work, env):
    return subprocess.run(
        ["bash", str(work / "deploy.sh"), "version"], env=env, capture_output=True, text=True, timeout=30
    )


def test_deploy_without_settings_names_all_three(copy):
    work, env, calls = copy
    result = run(work, env)
    assert result.returncode == 1
    for name in VARS:
        assert name in result.stderr
    assert "deploy.env" in result.stderr
    assert not calls.exists(), "nothing may reach the host before the settings are known"


def test_deploy_env_file_is_read(copy):
    work, env, calls = copy
    (work / "deploy.env").write_text(
        ": \"${IOS2HA_HOST:=file-host}\"\n"
        ": \"${IOS2HA_SRC:=/srv/src}\"\n"
        ": \"${IOS2HA_STACK:=/srv/stack}\"\n"
    )
    result = run(work, env)
    assert result.returncode == 0, result.stderr
    assert "file-host" in calls.read_text()


def test_environment_wins_over_deploy_env(copy):
    work, env, calls = copy
    (work / "deploy.env").write_text(
        ": \"${IOS2HA_HOST:=file-host}\"\n"
        ": \"${IOS2HA_SRC:=/srv/src}\"\n"
        ": \"${IOS2HA_STACK:=/srv/stack}\"\n"
    )
    result = run(work, {**env, "IOS2HA_HOST": "env-host"})
    assert result.returncode == 0, result.stderr
    assert "env-host" in calls.read_text() and "file-host" not in calls.read_text()


def test_example_settings_file_lists_every_variable():
    example = (SCRIPT.parent / "deploy.env.example").read_text()
    for name in VARS:
        assert name in example
