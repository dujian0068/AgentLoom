import hashlib
import multiprocessing
import os
import stat
import time
from pathlib import Path

import pytest
from agentloom_runtime.workspace_store import PosixWorkspaceStore


@pytest.fixture
def workspace(tmp_path):
    return PosixWorkspaceStore(tmp_path / "work" / "session")


def test_round_trip_lines_hash_permissions_and_directory_creation(workspace):
    result = workspace.write("reports/output.txt", "一\ntwo\nthree\n", expected_sha256="")
    raw = "一\ntwo\nthree\n".encode()
    assert result == {
        "file": "reports/output.txt",
        "path": "reports/output.txt",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }
    assert workspace.read("reports/output.txt", offset=2, limit=1) == {
        "path": "reports/output.txt",
        "content": "two\n",
        "start_line": 2,
        "end_line": 2,
        "total_lines": 3,
        "truncated": True,
        "sha256": result["sha256"],
        "size": len(raw),
    }
    assert workspace.read_bytes("reports/output.txt") == raw
    assert workspace.read("reports/output.txt", offset=50)["content"] == ""
    assert stat.S_IMODE((workspace.root / "reports").stat().st_mode) == 0o700
    assert stat.S_IMODE((workspace.root / "reports/output.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700
    assert workspace.lock_root not in workspace.root.parents
    assert not any(path.name.endswith(".lock") for path in workspace.root.rglob("*"))


def test_cas_edit_delete_and_ambiguity(workspace):
    original = workspace.write("sample", "old old")
    with pytest.raises(FileExistsError):
        workspace.write("sample", "bad", expected_sha256="")
    with pytest.raises(ValueError, match="precondition"):
        workspace.write("sample", "bad", expected_sha256="0" * 64)
    with pytest.raises(ValueError, match="ambiguous"):
        workspace.edit("sample", "old", "new")
    with pytest.raises(ValueError, match="not found"):
        workspace.edit("sample", "missing", "new")
    changed = workspace.edit(
        "sample", "old", "new", replace_all=True, expected_sha256=original["sha256"]
    )
    assert workspace.read("sample")["content"] == "new new"
    with pytest.raises(ValueError, match="precondition"):
        workspace.delete("sample", expected_sha256=original["sha256"])
    workspace.delete("sample", expected_sha256=changed["sha256"])
    with pytest.raises(FileNotFoundError):
        workspace.read("sample")
    with pytest.raises(FileNotFoundError):
        workspace.edit("missing", "a", "b")
    with pytest.raises(FileNotFoundError):
        workspace.write("missing", "a", expected_sha256=changed["sha256"])
    workspace.mkdir("nested/empty")
    with pytest.raises(ValueError):
        workspace.delete("nested")


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../secret",
        "a/../../secret",
        "a/../secret",
        "C:\\secret",
        "a\\b",
        "a\x00b",
        "",
        "a:",
        "a/" * 34 + "file",
    ],
)
def test_unsafe_paths_rejected_for_every_operation(workspace, path):
    for action in (
        lambda: workspace.read(path),
        lambda: workspace.write(path, "x"),
        lambda: workspace.edit(path, "a", "b"),
        lambda: workspace.delete(path),
        lambda: workspace.mkdir(path),
        lambda: workspace.stat(path),
        lambda: workspace.list(path),
        lambda: workspace.grep("x", path=path),
        lambda: workspace.glob("*", path=path),
    ):
        with pytest.raises(ValueError):
            action()


def test_links_fifo_and_devices_cannot_cross_boundary(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret"
    secret.write_text("private")
    (workspace.root / "parent").symlink_to(outside, target_is_directory=True)
    (workspace.root / "leaf").symlink_to(secret)
    os.link(secret, workspace.root / "hard")
    os.mkfifo(workspace.root / "pipe")
    for name in ("parent/secret", "leaf", "hard", "pipe"):
        for action in (
            lambda name=name: workspace.read(name),
            lambda name=name: workspace.write(name, "bad"),
            lambda name=name: workspace.edit(name, "private", "bad"),
            lambda name=name: workspace.delete(name),
            lambda name=name: workspace.stat(name),
        ):
            with pytest.raises(ValueError):
                action()
    assert secret.read_text() == "private"
    assert workspace.list()["entries"] == []
    assert workspace.glob("**/*")["paths"] == []
    assert workspace.grep("private")["matches"] == []


def test_root_and_lock_root_are_rechecked_without_symlink_resolution(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        PosixWorkspaceStore(alias / "session")
    with pytest.raises(ValueError):
        PosixWorkspaceStore(actual / "session", lock_root=alias / "locks")
    workspace = PosixWorkspaceStore(actual / "session")
    workspace.write("file", "safe")
    moved = actual / "old"
    workspace.root.rename(moved)
    workspace.root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace.read("file")
    with pytest.raises(ValueError):
        workspace.prepare()
    with pytest.raises(ValueError):
        PosixWorkspaceStore(actual, lock_root=actual / "locks")


def test_reserved_namespace_and_hidden_files(tmp_path):
    workspace = PosixWorkspaceStore(tmp_path / "work", reserved_paths=("subagents",))
    workspace.write(".hidden", "secret")
    workspace.write("normal", "value")
    (workspace.root / "subagents").mkdir()
    (workspace.root / "subagents/other").write_text("secret")
    assert [item["path"] for item in workspace.list()["entries"]] == ["normal"]
    assert workspace.glob("**/*", include_hidden=True)["paths"] == [".hidden", "normal"]
    assert [item["path"] for item in workspace.grep("secret", include_hidden=True)["matches"]] == [
        ".hidden"
    ]
    for action in (
        lambda: workspace.write("subagents/other", "bad"),
        lambda: workspace.list("subagents"),
        lambda: workspace.stat("subagents"),
        lambda: workspace.grep("secret", path="subagents"),
    ):
        with pytest.raises(ValueError, match="reserved"):
            action()


def test_glob_and_literal_or_bounded_regex_search(workspace):
    workspace.write("top.py", "begin\nAlpha value\nend\n")
    workspace.write("src/nested.py", "literal a.b\nalpha 123\n")
    workspace.write("src/file.md", "Alpha docs\n")
    assert workspace.glob("*.py")["paths"] == ["top.py"]
    assert workspace.glob("**/*.py")["paths"] == ["src/nested.py", "top.py"]
    assert workspace.glob("*.py", path="src")["paths"] == ["src/nested.py"]
    assert len(workspace.list(recursive=True)["entries"]) == 4
    assert workspace.list(limit=1)["truncated"]
    found = workspace.grep("alpha", glob="*.py", case_sensitive=False, context_lines=1)
    assert len(found["matches"]) == 2
    match = next(value for value in found["matches"] if value["path"] == "top.py")
    assert match == {
        "path": "top.py",
        "line": 2,
        "text": "Alpha value",
        "before": ["begin"],
        "after": ["end"],
    }
    assert found["scanned_files"] == 2
    assert workspace.grep("a.b")["matches"][0]["text"] == "literal a.b"
    assert len(workspace.grep(r"alpha\s+\d+", literal=False)["matches"]) == 1
    assert workspace.grep("alpha", case_sensitive=False, limit=1)["truncated"]
    assert workspace.grep("alpha", path="src/nested.py")["matches"][0]["line"] == 2
    with pytest.raises(ValueError, match="regular expression"):
        workspace.grep("[", literal=False)
    workspace.write("hostile.txt", "a" * 50000 + "!")
    started = time.monotonic()
    result = workspace.grep("(a+)+$", path="hostile.txt", literal=False)
    assert result["truncated"] is True
    assert time.monotonic() - started < 2


def test_file_scan_output_and_depth_budgets(workspace, monkeypatch):
    for index in range(8):
        workspace.write(f"item{index}", "match\n" * 5)
    monkeypatch.setattr(workspace, "MAX_WALK_ENTRIES", 3)
    assert len(workspace.list()["entries"]) == 3
    assert workspace.list()["truncated"]
    assert workspace.glob("*")["truncated"]
    monkeypatch.setattr(workspace, "MAX_WALK_ENTRIES", 5000)
    monkeypatch.setattr(workspace, "MAX_SCAN_FILES", 2)
    result = workspace.grep("match")
    assert result["truncated"]
    assert result["scanned_files"] == 2
    monkeypatch.setattr(workspace, "MAX_OUTPUT_CHARS", 7)
    assert workspace.grep("match")["truncated"]
    assert len(workspace.read("item0", max_chars=7)["content"]) == 7
    monkeypatch.setattr(workspace, "MAX_FILE_BYTES", 5)
    with pytest.raises(ValueError):
        workspace.write("oversized", "123456")
    with pytest.raises(ValueError):
        workspace.read_bytes("item0", max_bytes=5)
    assert workspace.grep("match")["truncated"]


def test_binary_data_and_failed_atomic_replace(workspace, monkeypatch):
    binary = workspace.root / "binary"
    binary.write_bytes(b"\xff\x00")
    assert workspace.read_bytes("binary") == b"\xff\x00"
    with pytest.raises(ValueError, match="UTF-8"):
        workspace.read("binary")
    assert workspace.grep("x")["truncated"]
    workspace.write("file", "original")

    def fail(*args, **kwargs):
        raise OSError("replace failure")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace failure"):
        workspace.write("file", "changed")
    assert workspace.read("file")["content"] == "original"
    assert not list(workspace.root.glob(".agentloom-tmp-*"))


def _race_create(root, queue, gate):
    workspace = PosixWorkspaceStore(Path(root))
    gate.wait(10)
    try:
        workspace.write("same.txt", str(os.getpid()), expected_sha256="")
        queue.put("created")
    except FileExistsError:
        queue.put("exists")


def _hold_lock(root, ready, done):
    workspace = PosixWorkspaceStore(Path(root))
    with workspace.lock():
        ready.set()
        done.wait(10)


def test_cross_process_serialization_and_lock_timeout(workspace):
    context = multiprocessing.get_context("spawn")
    queue, gate = context.Queue(), context.Event()
    processes = [
        context.Process(target=_race_create, args=(str(workspace.root), queue, gate))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    gate.set()
    assert sorted(queue.get(timeout=15) for _ in processes) == ["created", "exists", "exists"]
    for process in processes:
        process.join(10)
        assert process.exitcode == 0
    ready, done = context.Event(), context.Event()
    process = context.Process(target=_hold_lock, args=(str(workspace.root), ready, done))
    process.start()
    try:
        assert ready.wait(10)
        workspace.LOCK_TIMEOUT = 0.06
        with pytest.raises(TimeoutError, match="lock timed out"):
            workspace.write("blocked", "no")
    finally:
        done.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_quarantine_is_outside_mount_and_blocks_future_mutations(workspace):
    workspace.write("existing", "safe to inspect")
    with workspace.lock():
        workspace.quarantine()
        workspace.quarantine()
    assert workspace.read("existing")["content"] == "safe to inspect"
    assert workspace.list()["entries"][0]["path"] == "existing"
    for action in (
        lambda: workspace.write("new", "x"),
        lambda: workspace.edit("existing", "safe", "bad"),
        lambda: workspace.delete("existing"),
        lambda: workspace.mkdir("newdir"),
    ):
        with pytest.raises(ValueError, match="执行状态未知"):
            action()
    with pytest.raises(ValueError, match="执行状态未知"):
        with workspace.lock():
            pytest.fail("quarantined workspace acquired")
    marker = workspace.lock_root / workspace._quarantine_name
    assert marker.exists()
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    with pytest.raises(ValueError):
        workspace.delete("../.agentloom-locks/" + workspace._quarantine_name)
    assert marker.exists()


def test_stable_lock_identity_across_mount_paths(tmp_path):
    locks = tmp_path / "shared-locks"
    first = PosixWorkspaceStore(
        tmp_path / "node-a", lock_root=locks, lock_key="volume/space/agent/session/main"
    )
    second = PosixWorkspaceStore(
        tmp_path / "node-b", lock_root=locks, lock_key="volume/space/agent/session/main"
    )
    assert first._lock_name == second._lock_name
    second.LOCK_TIMEOUT = 0.02
    with first.lock():
        with pytest.raises(TimeoutError):
            with second.lock():
                pytest.fail("same namespace acquired twice")
        first.quarantine()
    with pytest.raises(ValueError, match="执行状态未知"):
        second.write("no", "blocked on either mount")


def _rewrite_repeatedly(root, ready, done):
    workspace = PosixWorkspaceStore(Path(root))
    ready.set()
    for index in range(25):
        workspace.write("atomic", ("a" if index % 2 else "b") * 65536)
    done.set()


def test_atomic_readers_never_observe_partial_replacement(workspace):
    workspace.write("atomic", "a" * 65536)
    context = multiprocessing.get_context("spawn")
    ready, done = context.Event(), context.Event()
    process = context.Process(target=_rewrite_repeatedly, args=(str(workspace.root), ready, done))
    process.start()
    try:
        assert ready.wait(10)
        deadline = time.monotonic() + 10
        while not done.is_set():
            assert time.monotonic() < deadline
            assert workspace.read_bytes("atomic") in (b"a" * 65536, b"b" * 65536)
    finally:
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join()
    assert process.exitcode == 0


def test_shared_lock_rejects_forged_links_and_fifo(workspace, tmp_path):
    target = tmp_path / "outside-lock"
    target.write_text("untouched")
    lock_file = workspace.lock_root / workspace._lock_name
    lock_file.symlink_to(target)
    with pytest.raises(ValueError):
        workspace.write("bad", "x")
    lock_file.unlink()
    os.link(target, lock_file)
    with pytest.raises(ValueError):
        workspace.write("bad", "x")
    lock_file.unlink()
    os.mkfifo(lock_file)
    with pytest.raises(ValueError):
        workspace.write("bad", "x")
    assert target.read_text() == "untouched"


def test_execution_marker_requires_exact_owner_and_confirmed_finish(workspace):
    workspace.write("inspect", "still readable")
    with workspace.lock():
        workspace.begin_execution("agentloom-owner-a")
        marker = workspace.lock_root / workspace._quarantine_name
        original = marker.read_bytes()
        workspace.quarantine()
        assert marker.read_bytes() == original
        assert stat.S_IMODE(marker.stat().st_mode) == 0o600
        with pytest.raises(ValueError, match="所有者不匹配"):
            workspace.finish_execution("agentloom-owner-b")
        with pytest.raises(ValueError, match="未确认"):
            workspace.begin_execution("agentloom-owner-b")
        assert workspace.read("inspect")["content"] == "still readable"
        workspace.finish_execution("agentloom-owner-a")
    assert not marker.exists()
    workspace.write("after", "unlocked after cleanup")
    assert workspace.read("after")["content"] == "unlocked after cleanup"


def _crash_with_live_container_marker(root):
    workspace = PosixWorkspaceStore(Path(root))
    with workspace.lock():
        workspace.begin_execution("agentloom-crashed-worker")
        os._exit(17)


def test_process_crash_leaves_marker_blocking_fresh_worker(workspace):
    workspace.write("inspect", "original")
    context = multiprocessing.get_context("spawn")
    process = context.Process(target=_crash_with_live_container_marker, args=(str(workspace.root),))
    process.start()
    process.join(10)
    try:
        assert process.exitcode == 17
        replacement = PosixWorkspaceStore(workspace.root)
        with pytest.raises(ValueError, match="执行状态未知"):
            replacement.write("must-not-race", "blocked")
        with pytest.raises(ValueError, match="执行状态未知"):
            with replacement.lock():
                pytest.fail("crashed worker lock must remain quarantined")
        assert replacement.read("inspect")["content"] == "original"
        assert (workspace.lock_root / workspace._quarantine_name).exists()
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_partial_or_generic_marker_cannot_be_cleared_by_execution_owner(workspace):
    marker = workspace.lock_root / workspace._quarantine_name
    with workspace.lock():
        workspace.quarantine()
        with pytest.raises(ValueError, match="不完整"):
            workspace.finish_execution("agentloom-owner")
    assert marker.exists()
    for invalid in ("", "../other", "name/other", "name\nother", "x" * 129):
        with pytest.raises(ValueError, match="identity"):
            workspace.begin_execution(invalid)
        with pytest.raises(ValueError, match="identity"):
            workspace.finish_execution(invalid)


def test_read_accepts_preopen_stat_from_atomically_replaced_unlinked_inode(workspace, monkeypatch):
    workspace.write("atomic", "complete new content")
    original_stat = os.stat
    observations = []

    def stat_during_replace(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == "atomic" and kwargs.get("follow_symlinks") is False:
            # APFS can return the inode selected before rename with nlink=0.
            # open() must then validate whichever inode is currently bound.
            values = list(info)
            values[3] = 0
            observations.append(True)
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "stat", stat_during_replace)
    assert workspace.read("atomic")["content"] == "complete new content"
    assert observations


def test_preopen_unlinked_metadata_does_not_skip_descriptor_hardlink_validation(
    workspace, tmp_path, monkeypatch
):
    secret = tmp_path / "secret"
    secret.write_text("outside")
    os.link(secret, workspace.root / "linked")
    original_stat = os.stat

    def stale_metadata(path, *args, **kwargs):
        info = original_stat(path, *args, **kwargs)
        if path == "linked" and kwargs.get("follow_symlinks") is False:
            values = list(info)
            values[3] = 0
            return os.stat_result(values)
        return info

    monkeypatch.setattr(os, "stat", stale_metadata)
    with pytest.raises(ValueError, match="hard links"):
        workspace.read_bytes("linked")
