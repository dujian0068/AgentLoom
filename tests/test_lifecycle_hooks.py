"""Lifecycle recovery reuses a finished Loop and retains its real outcome."""

import asyncio
import copy

import pytest
from agentloom_runtime.hooks import Continue, HookFailed, HookRejected, PatchOutput, Reject
from agentloom_runtime.lifecycle_hooks import RuntimeLifecycle
from test_harness import answer
from test_runtime_modules import FakeGateway, make
from test_tool_hooks import hook_manager


def test_run_before_rejection_prevents_model_execution(tmp_path):
    async def deny(ctx, payload):
        return Reject("not_ready", "本次运行未获授权")

    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, hooks=hook_manager(("run.before", deny, {})))

    async def run():
        try:
            with pytest.raises(HookRejected):
                await runtime.execute("task")
            operation = runtime.state["lifecycles"]["main"]["operation"]
            assert operation["actual_status"] == "not_started"
            assert "raw_output" not in operation
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not gateway.requests


def test_run_after_cannot_rewrite_completed_answer(tmp_path):
    async def rewrite(ctx, payload):
        return PatchOutput({"output": "rewritten result"})

    runtime = make(tmp_path, hooks=hook_manager(("run.after", rewrite, {})))

    async def run():
        try:
            with pytest.raises(HookFailed):
                await runtime.execute("task")
            operation = runtime.state["lifecycles"]["main"]["operation"]
            assert operation["actual_status"] == "succeeded"
            assert operation["raw_output"]["output"] == "done"
        finally:
            await runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("supplement", ["", "继续处理补充资料"])
def test_run_after_recovery_does_not_repeat_loop_or_lose_new_input(tmp_path, supplement):
    seen, diagnostics, saved = [], [], []

    async def after(ctx, payload):
        seen.append(payload["output"])
        if len(seen) == 1:
            raise RuntimeError("temporary postprocessing failure")
        return Continue()

    async def error(ctx, payload):
        if payload["kind"] == "run":
            diagnostics.append(dict(payload))
        return Continue()

    def hooks():
        return hook_manager(
            ("run.after", after, {}),
            ("operation.error", error, {"definition": {"observation": True}}),
        )

    async def run():
        first_gateway = FakeGateway([answer("original result")])
        first = make(
            tmp_path,
            model_gateway=first_gateway,
            hooks=hooks(),
            save=lambda state: saved.append(copy.deepcopy(state)),
        )
        try:
            with pytest.raises(HookFailed):
                await first.execute("task")
            checkpoint = copy.deepcopy(saved[-1])
            assert len(first_gateway.requests) == 1
        finally:
            await first.close()
        gateway = FakeGateway([answer("new result")])
        resumed = make(tmp_path, model_gateway=gateway, checkpoint=checkpoint, hooks=hooks())
        try:
            resumed.resume(supplement)
            output = await resumed.execute("task")
            assert output == ("new result" if supplement else "original result")
            assert len(gateway.requests) == (1 if supplement else 0)
            assert resumed.state["frames"]["main"]["outcome"] == "complete"
            assert not resumed.state["frames"]["main"]["context"]["pending_inputs"]
            if supplement:
                assert any(
                    supplement in (message.get("content") or "")
                    for message in gateway.requests[0].messages
                )
        finally:
            await resumed.close()

    asyncio.run(run())
    assert seen[:2] == ["original result", "original result"]
    assert diagnostics[0]["actual_status"] == "succeeded"
    assert diagnostics[0]["stage"] == "after"


def test_lifecycle_cleanup_cannot_replace_primary_failure():
    async def broken_observer(ctx, payload):
        raise RuntimeError("diagnostic failure")

    async def action():
        raise ValueError("primary action failure")

    manager = hook_manager(
        ("operation.error", broken_observer, {"definition": {"observation": True}}),
        ("operation.finally", broken_observer, {"definition": {"observation": True}}),
    )
    lifecycle = RuntimeLifecycle(manager)
    record, saved = {"operation_id": "life-1"}, []

    async def run():
        try:
            with pytest.raises(ValueError, match="primary action failure"):
                await lifecycle.run(
                    "run",
                    "main",
                    "task",
                    action,
                    record=record,
                    save=lambda: saved.append(copy.deepcopy(record)),
                )
        finally:
            await manager.aclose()

    asyncio.run(run())
    operation = record["operation"]
    assert operation["actual_status"] == "unknown"
    assert operation["attempts"][0]["error"]["records"][0]["status"] == "ignored"
    assert operation["attempts"][0]["finally"]["records"][0]["status"] == "ignored"
    assert saved[-1] == record
