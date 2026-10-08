"""Tool hooks preserve the actual operation separately from the model's result view."""

import asyncio
import copy
import json

import pytest
from agentloom_runtime import provider
from agentloom_runtime.execution_services import build_tool_context
from agentloom_runtime.handlers.mcp import definition as mcp_definition
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookError,
    HookFailed,
    HookManager,
    HookRegistry,
    PatchInput,
    PatchOutput,
    Reject,
)
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import (
    TOOL_REQUEST_TOPIC,
    ToolDefinition,
    ToolRequest,
    parameters,
)
from agentloom_runtime.tool_runtime import ToolRegistry, ToolRuntime
from test_harness import answer, call, decision, snapshot


def hook_manager(*items):
    registry = HookRegistry()
    bindings = []
    for index, (point, handler, options) in enumerate(items):
        hook_id = f"test-{index}"
        registry.register(
            HookDefinition(
                hook_id, "v1", handler, replay_safe=True, **options.get("definition", {})
            )
        )
        bindings.append(HookBinding(hook_id, hook_id, point, **options.get("binding", {})))
    return HookManager(registry, bindings)


def context_for(
    tmp_path, *, pending=None, events=None, saves=None, instance="main", authorize=None
):
    pending = pending if pending is not None else {}
    events = events if events is not None else []

    async def no_child(*args):
        raise AssertionError("unexpected delegation")

    return build_tool_context(
        config=snapshot()["config"],
        frame={"plan": []},
        state={"frames": {}, "delegations": 0, "citations": {}},
        pending=pending,
        instance=instance,
        root_workspace=tmp_path,
        emit=lambda kind, payload: events.append((kind, payload)),
        persist=lambda: saves.append(copy.deepcopy(pending)) if saves is not None else None,
        run_child=no_child,
        authorize_tool=authorize,
    )


def tool_runtime(handler, hooks, *, resume_inflight=False, **kwargs):
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            "lookup",
            "Lookup a value",
            parameters({"limit": {"type": "integer", "minimum": 1}}, ["limit"]),
            handler,
            evidence_fields=("limit",),
            implementation_id="lookup/v1",
            tool_id="stable-lookup",
            resume_inflight=resume_inflight,
        )
    )
    return ToolRuntime(snapshot(), registry, hooks=hooks, **kwargs)


async def request(runtime, context, *, uncertain=False):
    return await runtime.bus.request(
        TOOL_REQUEST_TOPIC,
        ToolRequest("call-1", "lookup", '{"limit": 50}', uncertain=uncertain),
        context=context,
        correlation_id="tool-operation-1",
    )


def test_tool_hooks_patch_validated_input_and_separate_raw_output(tmp_path):
    calls, seen, saves, pending, authorization = [], [], [], {}, []

    async def before(ctx, data):
        seen.append((ctx.run_id, ctx.instance_id, data["tool_id"]))
        assert "secret" not in data and "config" not in data
        with pytest.raises(TypeError):
            data["arguments"]["limit"] = 1
        return PatchInput({"arguments": {"limit": 3}})

    async def after(ctx, data):
        assert pending["__tool_runtime_operation"]["raw_output"]["value"] == {"secret": "private"}
        assert saves[-1]["__tool_runtime_operation"]["raw_output"]["value"] == {"secret": "private"}
        return PatchOutput({"value": {"summary": "safe result"}})

    async def handler(context, args):
        assert context.runtime_state is None
        calls.append(args)
        return {"secret": "private"}

    runtime = tool_runtime(
        handler,
        hook_manager(("tool.before", before, {}), ("tool.after", after, {})),
        hook_scope={"run_id": "run-1", "published_revision": "revision-1"},
    )
    context = context_for(
        tmp_path,
        pending=pending,
        saves=saves,
        instance="sub-1",
        authorize=lambda definition: authorization.append(definition.name),
    )

    async def run():
        try:
            outcome = await request(runtime, context)
            assert outcome.value == {"summary": "safe result"}
            assert outcome.raw_value == {"secret": "private"}
            assert outcome.has_raw_value and outcome.status == "succeeded"
            assert outcome.evidence_arguments == {"limit": 3}
        finally:
            await runtime.close()

    asyncio.run(run())
    assert calls == [{"limit": 3}]
    assert seen == [("run-1", "sub-1", "stable-lookup")]
    assert len(authorization) >= 2


@pytest.mark.parametrize("patch", [{"arguments": {"limit": "invalid"}}, {"name": "other"}])
def test_before_patch_cannot_bypass_schema_or_change_tool(tmp_path, patch):
    calls = []

    async def before(ctx, data):
        return PatchInput(patch)

    async def handler(context, args):
        calls.append(args)

    runtime = tool_runtime(handler, hook_manager(("tool.before", before, {})))

    async def run():
        try:
            with pytest.raises(HookError):
                await request(runtime, context_for(tmp_path))
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not calls


def test_rejected_tool_returns_model_feedback_without_invoking_handler(tmp_path):
    calls = []

    async def before(ctx, data):
        return Reject("approval_required", "需要授权")

    async def handler(context, args):
        calls.append(args)

    runtime = tool_runtime(handler, hook_manager(("tool.before", before, {})))

    async def run():
        try:
            result = await request(runtime, context_for(tmp_path))
            assert result.status == "failed"
            assert result.value["code"] == "hook_rejected"
            assert "需要授权" in result.value["error"]
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not calls


def test_failed_after_hook_recovers_saved_result_without_repeating_tool(tmp_path):
    pending, calls, after_calls = {}, [], []

    async def after(ctx, data):
        after_calls.append(True)
        if len(after_calls) == 1:
            raise ValueError("transient projection failure")
        return PatchOutput({"value": {"display": "done"}})

    async def handler(context, args):
        calls.append(args)
        context.invocation.set("handler_cursor", 2)
        return {"stored": "real effect"}

    manager = hook_manager(("tool.after", after, {}))

    async def run():
        runtime = tool_runtime(handler, manager)
        try:
            with pytest.raises(HookError):
                await request(runtime, context_for(tmp_path, pending=pending))
            saved = copy.deepcopy(pending)
            assert saved["__tool_runtime_operation"]["raw_output"]["value"] == {
                "stored": "real effect"
            }
            assert saved["handler_cursor"] == 2
        finally:
            await runtime.close()
        resumed = tool_runtime(handler, manager)
        try:
            result = await request(resumed, context_for(tmp_path, pending=saved), uncertain=True)
            assert result.value == {"display": "done"}
            assert result.raw_value == {"stored": "real effect"}
        finally:
            await resumed.close()

    asyncio.run(run())
    assert len(calls) == 1 and len(after_calls) == 2


def test_after_cannot_change_actual_success_or_evidence(tmp_path):
    pending = {}

    async def after(ctx, data):
        return PatchOutput({"status": "failed", "evidence_arguments": {}})

    async def handler(context, args):
        return {"done": True}

    runtime = tool_runtime(handler, hook_manager(("tool.after", after, {})))

    async def run():
        try:
            with pytest.raises(HookError):
                await request(runtime, context_for(tmp_path, pending=pending))
        finally:
            await runtime.close()

    asyncio.run(run())
    raw = pending["__tool_runtime_operation"]["raw_output"]
    assert raw == {
        "value": {"done": True},
        "status": "succeeded",
        "evidence_arguments": {"limit": 50},
    }


def test_handler_failure_keeps_real_failure_and_runs_error_hooks(tmp_path):
    observed = []

    async def error(ctx, data):
        observed.append(data)
        return Continue()

    async def after(ctx, data):
        return PatchOutput({"value": {"display": "cannot finish"}})

    async def handler(context, args):
        raise ValueError("actual service error")

    runtime = tool_runtime(
        handler,
        hook_manager(
            ("operation.error", error, {"definition": {"observation": True}}),
            ("tool.after", after, {}),
        ),
    )

    async def run():
        try:
            outcome = await request(runtime, context_for(tmp_path))
            assert outcome.status == "failed"
            assert outcome.value == {"display": "cannot finish"}
            assert outcome.raw_value == {"error": "actual service error"}
        finally:
            await runtime.close()

    asyncio.run(run())
    assert observed


def test_cancelled_tool_does_not_blindly_repeat_on_recovery(tmp_path):
    pending, calls = {}, []

    async def run():
        started = asyncio.Event()

        async def handler(context, args):
            calls.append(args)
            started.set()
            await asyncio.Event().wait()

        runtime = tool_runtime(handler, hook_manager())
        try:
            task = asyncio.create_task(request(runtime, context_for(tmp_path, pending=pending)))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await runtime.close()
        resumed = tool_runtime(handler, hook_manager())
        try:
            result = await request(resumed, context_for(tmp_path, pending=pending), uncertain=True)
            assert result.status == "failed"
            assert "结果未知" in result.value["error"]
            assert pending["__tool_runtime_operation"]["actual_status"] == "unknown"
        finally:
            await resumed.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_handler_cannot_overwrite_runtime_journal_through_invocation_port(tmp_path):
    pending = {}
    context = context_for(tmp_path, pending=pending)
    context.runtime_state.set("operation", {"stage": "before"})
    for action in (
        lambda: context.invocation.get("__tool_runtime_operation"),
        lambda: context.invocation.set("__tool_runtime_operation", {}),
    ):
        with pytest.raises(ValueError, match="运行时"):
            action()
    assert pending == {"__tool_runtime_operation": {"stage": "before"}}


@pytest.mark.parametrize("change", ["schema", "authorization"])
def test_cached_prepared_input_is_revalidated_before_resumable_handler(tmp_path, change):
    pending, calls = {}, []

    async def run():
        started = asyncio.Event()

        async def handler(context, args):
            calls.append(args)
            started.set()
            await asyncio.Event().wait()

        runtime = tool_runtime(handler, hook_manager(), resume_inflight=True)
        try:
            task = asyncio.create_task(request(runtime, context_for(tmp_path, pending=pending)))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await runtime.close()
        # Restored final inputs cross both platform boundaries immediately before dispatch.
        if change == "schema":
            pending["__tool_runtime_operation"]["final_input"]["arguments"]["limit"] = "invalid"
        checks = []

        def authorize(definition):
            checks.append(True)
            if change == "authorization" and len(checks) > 1:
                raise ValueError("authorization changed before dispatch")

        resumed = tool_runtime(handler, hook_manager(), resume_inflight=True)
        try:
            result = await request(
                resumed,
                context_for(tmp_path, pending=pending, authorize=authorize),
                uncertain=True,
            )
            assert result.status == "failed"
            assert (
                "参数不合法" if change == "schema" else "authorization changed"
            ) in result.value["error"]
        finally:
            await resumed.close()

    asyncio.run(run())
    assert len(calls) == 1


def test_mcp_stable_identity_is_independent_of_function_alias():
    service = {"id": "resource-long-stable-id", "name": "Renamable label", "secret": "secret"}
    spec = {"name": "orders.search", "description": "Search orders", "schema": parameters({})}
    definition = mcp_definition(service, spec, lambda value: value)
    assert definition.tool_id == "mcp:resource-long-stable-id:orders.search"
    assert definition.name != definition.tool_id
    registry = ToolRegistry()
    registry.register(definition)
    assert registry.bindings()[0]["tool_id"] == definition.tool_id


def test_nested_hook_failure_is_not_masked_as_a_normal_tool_failure(tmp_path):
    async def handler(context, args):
        raise HookFailed("child hook failure must stop the parent operation")

    runtime = tool_runtime(handler, hook_manager())

    async def run():
        try:
            with pytest.raises(HookError):
                await request(runtime, context_for(tmp_path))
        finally:
            await runtime.close()

    asyncio.run(run())


def test_engine_restores_tool_postprocessing_and_keeps_raw_journal(tmp_path, monkeypatch):
    calls, projections, saved, model_messages = [], [], [], []

    async def handler(context, args):
        calls.append(True)
        return {"private": "raw tool output"}

    async def after(ctx, data):
        projections.append(True)
        if len(projections) == 1:
            raise ValueError("temporary projection failure")
        return PatchOutput({"value": {"public": "safe tool output"}})

    def configure(registry):
        registry.register(
            ToolDefinition(
                "lookup",
                "Lookup",
                parameters({}),
                handler,
                implementation_id="lookup/v1",
            )
        )

    async def model(model, messages, tools, secret):
        if not tools:
            return decision()
        model_messages.append(copy.deepcopy(messages))
        if len(model_messages) == 1:
            return call("lookup", {}, "lookup-call")
        assert json.loads(messages[-1]["content"]) == {"public": "safe tool output"}
        return answer("done")

    async def search(*args):
        return []

    def create(checkpoint=None):
        return create_engine(
            snapshot(),
            tmp_path,
            lambda *args: None,
            lambda value: value,
            search,
            checkpoint=checkpoint,
            save=lambda state: saved.append(copy.deepcopy(state)),
            configure_tools=configure,
            hooks=hook_manager(("tool.after", after, {})),
        )

    monkeypatch.setattr(provider, "chat", model)

    async def run():
        first = create()
        try:
            with pytest.raises(HookError):
                await first.execute("lookup")
            checkpoint = copy.deepcopy(saved[-1])
        finally:
            await first.close()
        resumed = create(checkpoint)
        try:
            resumed.resume()
            assert await resumed.execute("lookup") == "done"
            frame = resumed.state["frames"]["main"]
            records = [item for item in frame["context"]["records"] if item["kind"] == "tool"]
            assert json.loads(records[0]["message"]["content"]) == {"private": "raw tool output"}
            operation = next(iter(resumed.state["tool_operations"].values()))
            assert operation["raw_output"]["value"] == {"private": "raw tool output"}
            assert operation["effective_output"]["value"] == {"public": "safe tool output"}
        finally:
            await resumed.close()

    asyncio.run(run())
    assert len(calls) == 1 and len(projections) == 2
