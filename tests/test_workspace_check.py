"""The deployment checker stays independent of app configuration and user data."""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/check_workspace.py"


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("workspace_deployment_check", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def volume(tmp_path):
    root = tmp_path / "volume"
    root.mkdir()
    (root / ".agentloom-volume").write_text("test-volume\n")
    return root


def cli(tmp_path, *args, **settings):
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("AGENT_LOOM_WORKSPACE_")
    }
    environment.update(
        DB_BACKEND="postgresql",
        DB_HOST="not-a-database.invalid",
        DB_PASSWORD="not-a-real-secret",
        AGENT_LOOM_DATA=str(tmp_path / "must-not-create-data"),
        PYTHONDONTWRITEBYTECODE="1",
        **settings,
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_read_only_cli_does_not_load_dotenv_database_or_create_directories(tmp_path):
    root = volume(tmp_path)
    untouched = root / "user-file"
    untouched.write_text("must remain")
    (tmp_path / ".env").write_text(
        "AGENT_LOOM_WORKSPACE_ROOT=/invalid/from-dotenv\n"
        "AGENT_LOOM_WORKSPACE_VOLUME_ID=wrong\n"
        "AGENT_LOOM_DATA=/invalid/must-not-create\n"
    )
    response = cli(tmp_path, "--root", str(root), "--volume-id", "test-volume")
    assert response.returncode == 0, response.stderr
    result = json.loads(response.stdout)
    assert result["mode"] == "read_only"
    assert result["volume_checked"] is True
    assert result["checks"] == {}
    assert sorted(path.name for path in root.iterdir()) == [".agentloom-volume", "user-file"]
    assert untouched.read_text() == "must remain"
    assert not (tmp_path / "must-not-create-data").exists()


def test_cli_accepts_explicit_environment_and_probe_cleans_only_own_files(tmp_path):
    root = volume(tmp_path)
    existing = root / ".agentloom-probe-user"
    existing.mkdir()
    (existing / "value").write_text("keep")
    response = cli(
        tmp_path,
        "--probe",
        AGENT_LOOM_WORKSPACE_ROOT=str(root),
        AGENT_LOOM_WORKSPACE_VOLUME_ID="test-volume",
        AGENT_LOOM_WORKSPACE_BACKEND="shared_posix",
    )
    assert response.returncode == 0, response.stderr
    result = json.loads(response.stdout)
    assert result["scope"] == "single_node"
    assert result["checks"] == {
        "write_read": True,
        "atomic_replace": True,
        "advisory_lock_local": True,
        "cleaned": True,
    }
    assert sorted(path.name for path in root.iterdir()) == [
        ".agentloom-probe-user",
        ".agentloom-volume",
    ]
    assert (existing / "value").read_text() == "keep"
    assert not (tmp_path / "must-not-create-data").exists()


@pytest.mark.parametrize("kind", ["missing", "wrong", "symlink", "hardlink", "fifo", "large"])
def test_shared_marker_is_regular_bounded_and_matching(checker, tmp_path, kind):
    root = volume(tmp_path)
    marker = root / ".agentloom-volume"
    if kind == "missing":
        marker.unlink()
    elif kind == "wrong":
        marker.write_text("wrong")
    elif kind == "symlink":
        marker.rename(root / "real-marker")
        marker.symlink_to(root / "real-marker")
    elif kind == "hardlink":
        os.link(marker, root / "another-marker")
    elif kind == "fifo":
        marker.unlink()
        os.mkfifo(marker)
    else:
        marker.write_text("x" * 2000)
    before = set(path.name for path in root.iterdir())
    with pytest.raises((ValueError, OSError)):
        checker.check(str(root), "shared_posix", "test-volume", run_probe=True)
    assert set(path.name for path in root.iterdir()) == before


def test_checker_rejects_symlink_root_and_missing_root_without_mutation(checker, tmp_path):
    root = volume(tmp_path)
    link = tmp_path / "linked"
    link.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        checker.check(str(link), "shared_posix", "test-volume")
    missing = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        checker.check(str(missing), "local", None)
    assert not missing.exists()


def test_probe_failure_cleans_its_created_namespace(checker, tmp_path, monkeypatch):
    root = volume(tmp_path)

    def failed_replace(*args, **kwargs):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(checker.os, "replace", failed_replace)
    with pytest.raises(OSError):
        checker.check(str(root), "shared_posix", "test-volume", run_probe=True)
    assert [path.name for path in root.iterdir()] == [".agentloom-volume"]


def test_probe_namespace_collision_never_cleans_existing_files(checker, tmp_path, monkeypatch):
    root = volume(tmp_path)
    existing = root / ".agentloom-probe-collision"
    existing.mkdir()
    (existing / "value").write_text("keep")
    monkeypatch.setattr(checker, "uuid4", lambda: SimpleNamespace(hex="collision"))
    with pytest.raises(FileExistsError):
        checker.check(str(root), "shared_posix", "test-volume", run_probe=True)
    assert (existing / "value").read_text() == "keep"


def test_cli_requires_explicit_root_instead_of_reading_dotenv(tmp_path):
    (tmp_path / ".env").write_text("AGENT_LOOM_WORKSPACE_ROOT=/some/path\n")
    response = cli(tmp_path)
    assert response.returncode == 2
    assert "不会读取 .env" in response.stderr
    assert not (tmp_path / "must-not-create-data").exists()


def test_local_check_has_no_volume_claim(checker, tmp_path):
    result = checker.check(str(tmp_path), "local", None)
    assert result["volume_checked"] is False
    assert result["mode"] == "read_only"
