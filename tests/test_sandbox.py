"""Sandbox isolation/lifecycle contracts, without requiring a Docker daemon."""

import asyncio

import pytest
from agentloom_runtime import sandbox
from agentloom_runtime.workspace_store import PosixWorkspaceStore


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: "/usr/bin/docker")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 1001)
    monkeypatch.setattr(sandbox.os, "getegid", lambda: 1001)
    for key in ("AGENT_LOOM_DOCKER_HOST", "AGENT_LOOM_SANDBOX_IMAGE", "AGENT_LOOM_SANDBOX_RUNTIME"):
        monkeypatch.delenv(key, raising=False)
    return PosixWorkspaceStore(tmp_path / "workspace", lock_root=tmp_path / "private-locks")


class Process:
    def __init__(self, output=b"ok\n", *, eof=True, code=0):
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output)
        if eof:
            self.stdout.feed_eof()
        self.returncode = None
        self.code = code
        self.killed = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode

    async def communicate(self):
        value = await self.stdout.read()
        await self.wait()
        return value, None

    def kill(self):
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()


def test_arguments_mount_one_namespace_and_bound_skills_only(configured, tmp_path, monkeypatch):
    skill = tmp_path / "skill"
    skill.mkdir()
    monkeypatch.setenv("AGENT_LOOM_SANDBOX_RUNTIME", "runsc")
    arguments = sandbox.docker_arguments(
        "docker", configured.root, [{"id": "skill-1", "path": str(skill)}], "echo hi", "random-id"
    )
    mounts = [arguments[i + 1] for i, value in enumerate(arguments) if value == "--mount"]
    assert len(mounts) == 2
    assert mounts[0] == f"type=bind,src={configured.root},dst=/workspace,bind-propagation=rprivate"
    assert mounts[1].endswith("dst=/skills/skill-1,readonly,bind-propagation=rprivate")
    assert str(configured.lock_root) not in " ".join(arguments)
    assert arguments[arguments.index("--network") + 1] == "none"
    assert arguments[arguments.index("--user") + 1] == "1001:1001"
    assert arguments[arguments.index("--runtime") + 1] == "runsc"
    assert "--read-only" in arguments and "no-new-privileges" in arguments
    assert arguments[-1] == "umask 077\necho hi"
    assert "--pull" in arguments and arguments[arguments.index("--pull") + 1] == "never"


def test_remote_daemon_root_identity_and_symlink_skill_are_rejected(
    configured, tmp_path, monkeypatch
):
    monkeypatch.setenv("AGENT_LOOM_DOCKER_HOST", "tcp://example.test:2375")
    with pytest.raises(ValueError, match="Unix"):
        sandbox.docker_arguments("docker", configured.root, [], "true", "id")
    monkeypatch.delenv("AGENT_LOOM_DOCKER_HOST")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="root"):
        sandbox.docker_arguments("docker", configured.root, [], "true", "id")
    monkeypatch.setattr(sandbox.os, "geteuid", lambda: 1001)
    link = tmp_path / "linked-skill"
    link.symlink_to(configured.root, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        sandbox.docker_arguments(
            "docker", configured.root, [{"id": "skill", "path": str(link)}], "true", "id"
        )


def test_command_no_skills_random_names_and_scrubbed_environment(configured, monkeypatch):
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-pass-to-docker")
    monkeypatch.setenv("DOCKER_HOST", "tcp://untrusted-daemon:2375")
    monkeypatch.setenv("DOCKER_CONTEXT", "personal-context")

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)

    async def run():
        assert (await sandbox.command(configured, [], "echo hi"))["output"] == "ok\n"
        assert (await sandbox.command(configured, [], "echo hi"))["exit_code"] == 0

    asyncio.run(run())
    runs = [args for args, _ in calls if "run" in args]
    assert (
        len(runs) == 2
        and runs[0][runs[0].index("--name") + 1] != runs[1][runs[1].index("--name") + 1]
    )
    assert len([args for args, _ in calls if "rm" in args]) == 2
    for args, options in calls:
        assert args[2] == "unix:///var/run/docker.sock"
        assert set(options["env"]) == {"PATH", "HOME", "DOCKER_CONFIG", "LANG"}
        assert "must-not-pass" not in str(options)
    configured.write("after.txt", "available")


def test_cancel_kills_only_own_container_and_releases_lock(configured, monkeypatch):
    calls, processes = [], []

    async def run():
        entered = asyncio.Event()

        async def spawn(*args, **kwargs):
            calls.append(args)
            process = Process(b"", eof="run" not in args)
            processes.append(process)
            if "run" in args:
                entered.set()
            return process

        monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(sandbox.command(configured, [], "long work"))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert processes[0].killed
    assert calls[1][-1] == calls[0][calls[0].index("--name") + 1]
    configured.write("after.txt", "released")


def test_cancel_during_lock_acquisition_cannot_leave_lock_held(configured, monkeypatch):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)

    async def run():
        held = configured.lock()
        held.__enter__()
        task = asyncio.create_task(sandbox.command(configured, [], "true"))
        await asyncio.sleep(0.08)
        task.cancel()
        held.__exit__(None, None, None)
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert calls == []
    configured.write("after.txt", "released")


def test_unconfirmed_cleanup_quarantines_writes(configured, monkeypatch):
    async def spawn(*args, **kwargs):
        return Process(b"daemon unavailable", code=1) if "rm" in args else Process()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    configured.write("existing.txt", "inspect")
    with pytest.raises(RuntimeError, match="清理"):
        asyncio.run(sandbox.command(configured, [], "true"))
    assert configured.read("existing.txt")["content"] == "inspect"
    with pytest.raises(ValueError, match="执行状态未知"):
        configured.write("later.txt", "no")


def test_output_limit_stops_process_and_does_not_grow_unbounded(configured, monkeypatch):
    processes = []

    async def spawn(*args, **kwargs):
        process = Process(b"x" * (sandbox.MAX_OUTPUT_BYTES + 1)) if "run" in args else Process()
        processes.append(process)
        return process

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(RuntimeError, match="256KB"):
        asyncio.run(sandbox.command(configured, [], "large output"))
    assert processes[0].killed and len(processes) == 2
    configured.write("after.txt", "safe")


def test_absent_docker_never_falls_back_to_host(configured, monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="不会在宿主机"):
        asyncio.run(sandbox.command(configured, [], "touch escaped.txt"))
    assert not (configured.root / "escaped.txt").exists()


def test_process_launch_cancellation_reconciles_container_name(configured, monkeypatch):
    calls = []

    async def run():
        entered = asyncio.Event()
        finish_launch = asyncio.Event()

        async def spawn(*args, **kwargs):
            calls.append(args)
            if "run" in args:
                entered.set()
                await finish_launch.wait()
                return Process(b"", eof=False)
            return Process(b"No such container", code=1)

        monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(sandbox.command(configured, [], "start"))
        await entered.wait()
        task.cancel()
        finish_launch.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) == 2 and "rm" in calls[1]
    # A killed CLI and a missing container cannot rule out a daemon still
    # completing create. Keep the execution marker until an operator reconciles.
    with pytest.raises(ValueError, match="执行状态未知"):
        configured.write("after.txt", "unsafe")


def test_execution_marker_precedes_launch_and_is_cleared_after_cleanup(configured, monkeypatch):
    async def spawn(*args, **kwargs):
        with pytest.raises(ValueError, match="未确认的沙箱执行"):
            configured.begin_execution("another-container")
        return Process()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(sandbox.command(configured, [], "true"))
    configured.write("after.txt", "safe")


def test_naturally_finished_auto_removed_container_clears_marker(configured, monkeypatch):
    async def spawn(*args, **kwargs):
        return Process(b"No such container", code=1) if "rm" in args else Process()

    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    asyncio.run(sandbox.command(configured, [], "true"))
    configured.write("after.txt", "safe")
