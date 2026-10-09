"""Adversarial lifecycle ordering checks without starting a real container."""

import asyncio

import pytest
from agentloom_runtime import sandbox
from test_sandbox import Process
from test_sandbox import configured as configured


def test_cancelled_launch_is_settled_before_cleanup_can_unlock_scope(configured, monkeypatch):
    order = []

    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        launched = Process(b"", eof=False)

        async def spawn(*args, **kwargs):
            if "run" in args:
                order.append("launch-entered")
                entered.set()
                await release.wait()
                order.append("launch-handle-returned")
                return launched
            order.append("cleanup-entered")
            assert launched.killed
            return Process()

        monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
        task = asyncio.create_task(sandbox.command(configured, [], "work"))
        await entered.wait()
        task.cancel()
        # Allow cancellation to reach shield while the CLI handle is withheld.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert order == ["launch-entered"]
        assert not task.done()
        with pytest.raises(ValueError, match="执行状态未知"):
            configured.write("racing-writer", "forbidden")
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert order == ["launch-entered", "launch-handle-returned", "cleanup-entered"]
    configured.write("safe", "confirmed cleanup")


def test_failed_pipe_drain_keeps_execution_marker_and_releases_host_flock(configured, monkeypatch):
    calls = []
    actual_wait_for = asyncio.wait_for

    async def force_drain_timeout(awaitable, timeout):
        if timeout == 5:
            awaitable.close()
            raise TimeoutError("simulated blocked daemon CLI pipe")
        return await actual_wait_for(awaitable, timeout)

    async def spawn(*args, **kwargs):
        calls.append(args)
        if "run" in args:
            return Process(b"x" * (sandbox.MAX_OUTPUT_BYTES + 1))
        return Process()

    monkeypatch.setattr(sandbox.asyncio, "wait_for", force_drain_timeout)
    monkeypatch.setattr(sandbox.asyncio, "create_subprocess_exec", spawn)
    configured.write("inspect", "read remains available")
    with pytest.raises(TimeoutError):
        asyncio.run(sandbox.command(configured, [], "too much output"))
    assert len(calls) == 1  # Failed CLI drain must not pretend rm proved cleanup.
    assert configured.read("inspect")["content"] == "read remains available"
    with pytest.raises(ValueError, match="执行状态未知"):
        configured.write("after", "blocked")
    # The durable marker blocks future use, while the actual flock is released.
    marker = configured.lock_root / configured._quarantine_name
    marker.unlink()  # Simulated offline administrator reconciliation, test only.
    configured.write("after", "operator confirmed stopped")
