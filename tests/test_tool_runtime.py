"""Tool requests cross the bus without exposing implementations to the agent loop."""

import asyncio
import copy
import json

import pytest
from agentloom_runtime import mcp_tools, provider
from agentloom_runtime.event_bus import EventBus, UnknownTopicError
from agentloom_runtime.execution_services import build_tool_context
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import (
    TOOL_REQUEST_TOPIC,
    ToolDefinition,
    ToolPersistenceError,
    ToolRequest,
    parameters,
)
from agentloom_runtime.tool_runtime import ToolRegistry, ToolRuntime
from test_harness import answer, call, decision, runner, snapshot


async def no_search(*args):
    return []


def tool_context(tmp_path, events, config=None):
    async def no_child(*args):
        raise AssertionError("This request should not create a child")

    return build_tool_context(
        config=config or snapshot()["config"],
        frame={"plan": []},
        state={"frames": {}, "delegations": 0, "citations": {}},
        pending={},
        instance="main",
        root_workspace=tmp_path,
        emit=lambda kind, payload: events.append({"kind": kind, **payload}),
        persist=lambda: None,
        run_child=no_child,
    )


def test_new_registered_tool_runs_through_loop_and_correlated_bus(tmp_path, monkeypatch):
    observations, events, received = [], [], []

    async def convert(context, args):
        received.append((context.instance, args))
        return {"converted": args["amount"] * 2}

    def configure(registry):
        registry.register(
            ToolDefinition(
                name="custom_convert",
                description="Convert an amount using a separately registered capability",
                parameters=parameters({"amount": {"type": "integer"}}, ["amount"]),
                handler=convert,
            )
        )

    turns = 0

    async def fake_model(model, messages, tools, secret):
        nonlocal turns
        if not tools:
            return decision()
        assert "custom_convert" in {item["function"]["name"] for item in tools}
        turns += 1
        if turns == 1:
            return call("custom_convert", {"amount": 21}, "custom-call")
        assert messages[-1]["tool_call_id"] == "custom-call"
        assert json.loads(messages[-1]["content"]) == {"converted": 42}
        return answer("转换结果为 42")

    monkeypatch.setattr(provider, "chat", fake_model)
    runtime = create_engine(
        snapshot(),
        tmp_path,
        lambda kind, payload: events.append({"kind": kind, **payload}),
        lambda value: value,
        no_search,
        configure_tools=configure,
    )

    async def observe(event):
        observations.append(event)

    runtime.tools.bus.subscribe("request.started", observe)
    runtime.tools.bus.subscribe("request.completed", observe)

    async def run():
        try:
            assert await runtime.execute("转换 21") == "转换结果为 42"
            assert runtime.tools.bus.pending_count == 0
        finally:
            await runtime.close()

    asyncio.run(run())
    assert received == [("main", {"amount": 21})]
    assert [event.topic for event in observations] == ["request.started", "request.completed"]
    assert len({event.correlation_id for event in observations}) == 1
    tool_events = [event for event in events if event["kind"].startswith("tool.")]
    assert [event["kind"] for event in tool_events] == ["tool.started", "tool.completed"]
    assert {event["call_id"] for event in tool_events} == {"custom-call"}
    assert {event["request_id"] for event in tool_events} == {observations[0].correlation_id}


@pytest.mark.parametrize(
    ("name", "arguments", "expected_error"),
    [
        ("unknown_tool", {"count": 2}, "未授权"),
        ("count_items", {"count": "not-an-integer"}, "参数不合法"),
    ],
)
def test_rejected_request_returns_failed_without_executing_handler(
    tmp_path, name, arguments, expected_error
):
    events, executed = [], []
    registry = ToolRegistry()

    async def handler(context, args):
        executed.append(args)
        return "must not execute"

    registry.register(
        ToolDefinition(
            name="count_items",
            description="Count items",
            parameters=parameters({"count": {"type": "integer"}}, ["count"]),
            handler=handler,
        )
    )
    runtime = ToolRuntime(snapshot(), registry)

    async def run():
        try:
            result = await runtime.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("rejected-call", name, json.dumps(arguments)),
                context=tool_context(tmp_path, events),
                correlation_id="rejected-request",
            )
            assert result.status == "failed"
            assert expected_error in result.value["error"]
            assert runtime.bus.pending_count == 0
        finally:
            await runtime.close()

    asyncio.run(run())
    assert executed == []
    assert [event["kind"] for event in events] == ["tool.started", "tool.failed"]
    assert {event["call_id"] for event in events} == {"rejected-call"}
    assert {event["request_id"] for event in events} == {"rejected-request"}


def mcp_snapshot():
    snap = snapshot()
    service = {
        "id": "main-mcp",
        "name": "Main service",
        "secret": "test-secret",
        "schemas": [{"name": "lookup", "description": "Look up data", "schema": parameters({})}],
    }
    snap["tools"] = [service]
    snap["config"]["tools"] = [service["id"]]
    return snap, "mcp_main-mcp_lookup"


def test_child_cannot_use_main_resources_or_escape_its_workspace(tmp_path, monkeypatch):
    snap, mcp_name = mcp_snapshot()
    child = {
        "id": "worker",
        "name": "执行助手",
        "description": "执行已委派任务",
        "prompt": "SCOPED_CHILD",
        "skills": ["child-skill"],
        "tools": [],
        "wiki": [],
    }
    snap["config"]["subs"] = [child]
    snap["config"]["skills"] = ["main-skill"]
    snap["skills"] = [
        {
            "id": name,
            "name": name,
            "description": name,
            "content": "private instructions for " + name,
            "path": str(tmp_path / name),
        }
        for name in ("main-skill", "child-skill")
    ]
    main_actions = [
        call("skill_load", {"skill_id": "main-skill"}),
        call(mcp_name, {}),
        call("workspace_write", {"path": "shared.txt", "content": "main-only"}),
        call("delegate_task", {"subagent_id": "worker", "task": "隔离执行"}),
        answer("主任务完成"),
    ]
    child_actions = [
        call("skill_load", {"skill_id": "main-skill"}),
        call("skill_read", {"skill_id": "main-skill", "path": "SKILL.md"}),
        call(mcp_name, {}),
        call("workspace_read", {"path": "../../shared.txt"}),
        call("workspace_write", {"path": "shared.txt", "content": "child-only"}),
        answer("子任务完成"),
    ]
    mcp_calls = []

    async def fake_mcp(service, name, args, secret):
        mcp_calls.append((service["id"], name, secret))
        return {"is_error": False, "content": [{"type": "text", "text": "main result"}]}

    async def fake_model(model, messages, tools, secret):
        if not tools:
            return decision()
        if "SCOPED_CHILD" in messages[0]["content"]:
            visible = {item["function"]["name"] for item in tools}
            assert "skill_load" in visible  # Its own skill is available.
            assert mcp_name not in visible
            return child_actions.pop(0)
        return main_actions.pop(0)

    monkeypatch.setattr(provider, "chat", fake_model)
    monkeypatch.setattr(mcp_tools, "call", fake_mcp)
    runtime, events, _ = runner(tmp_path, snap)

    async def run():
        try:
            assert await runtime.execute("复杂任务") == "主任务完成"
        finally:
            await runtime.close()

    asyncio.run(run())
    assert mcp_calls == [("main-mcp", "lookup", "test-secret")]
    assert (tmp_path / "shared.txt").read_text() == "main-only"
    assert (tmp_path / "subagents/sub-1/shared.txt").read_text() == "child-only"
    failed = [event for event in events if event["kind"] == "tool.failed"]
    assert [event["name"] for event in failed] == [
        "skill_load",
        "skill_read",
        mcp_name,
        "workspace_read",
    ]
    assert all(event["instance"] == "sub-1" for event in failed)
    assert all(event["call_id"] and event["request_id"] for event in failed)
    child_results = [
        message["content"]
        for message in runtime.state["frames"]["sub-1"]["messages"]
        if message["role"] == "tool"
    ]
    assert "main-only" not in " ".join(child_results)
    assert "private instructions for main-skill" not in " ".join(child_results)


def test_mcp_error_is_a_failed_outcome_instead_of_success(tmp_path, monkeypatch):
    snap, name = mcp_snapshot()
    events = []

    async def fake_mcp(service, tool, args, secret):
        return {"is_error": True, "content": [{"type": "text", "text": "record not found"}]}

    monkeypatch.setattr(mcp_tools, "call", fake_mcp)
    runtime = create_engine(snap, tmp_path, lambda *args: None, lambda value: value, no_search)

    async def run():
        try:
            outcome = await runtime.tools.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("mcp-call", name, "{}"),
                context=tool_context(tmp_path, events, snap["config"]),
                correlation_id="mcp-request",
            )
            assert outcome.status == "failed"
            assert "record not found" in outcome.value["error"]
        finally:
            await runtime.close()

    asyncio.run(run())
    assert [event["kind"] for event in events] == ["tool.started", "tool.failed"]
    assert {event["request_id"] for event in events} == {"mcp-request"}


@pytest.mark.parametrize("finish", ["timeout", "cancel"])
def test_interrupted_handler_finishes_cleanup_and_has_no_background_side_effects(tmp_path, finish):
    events, side_effects, cleaned = [], [], []

    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(context, args):
            try:
                started.set()
                await release.wait()
                side_effects.append("external write")
                return "done"
            finally:
                await asyncio.sleep(0)
                cleaned.append(True)

        registry = ToolRegistry()
        registry.register(
            ToolDefinition(
                name="delayed_write",
                description="Wait before performing a side effect",
                parameters=parameters({}),
                handler=delayed,
                timeout=0.02 if finish == "timeout" else 60,
            )
        )
        runtime = ToolRuntime(snapshot(), registry)
        task = asyncio.create_task(
            runtime.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("delayed-call", "delayed_write", "{}"),
                context=tool_context(tmp_path, events),
                correlation_id="delayed-request",
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 1)
            if finish == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                outcome = await asyncio.wait_for(task, 1)
                assert outcome.status == "failed" and "超时" in outcome.value["error"]
            assert cleaned == [True]
            assert runtime.bus.pending_count == 0
            release.set()
            await asyncio.sleep(0)
            assert side_effects == []
        finally:
            await runtime.close()

    asyncio.run(run())
    assert [event["kind"] for event in events] == [
        "tool.started",
        "tool.cancelled" if finish == "cancel" else "tool.failed",
    ]
    assert {event["call_id"] for event in events} == {"delayed-call"}
    assert {event["request_id"] for event in events} == {"delayed-request"}


def test_legacy_main_pending_resumes_ready_tool_once(tmp_path, monkeypatch):
    runtime, _, _ = runner(tmp_path)
    tool_call = call("workspace_write", {"path": "legacy.txt", "content": "old checkpoint"})
    runtime.state["frames"]["main"] = {
        "task": "恢复旧主任务",
        "messages": [
            {"role": "system", "content": "完成任务"},
            {"role": "user", "content": "恢复旧主任务"},
            tool_call,
        ],
        "plan": [],
        "evidence": [],
        "iterations": 1,
        "pending": {"calls": tool_call["tool_calls"], "index": 0, "stage": "ready"},
        "candidate": None,
    }
    checkpoint = copy.deepcopy(runtime.state)

    async def fake_model(model, messages, tools, secret):
        if tools:
            assert json.loads(messages[-1]["content"]) == {"file": "legacy.txt"}
            return answer("旧任务已恢复")
        return decision()

    monkeypatch.setattr(provider, "chat", fake_model)
    resumed, events, _ = runner(tmp_path, state=checkpoint)
    resumed.resume()

    async def run():
        try:
            assert await resumed.execute("恢复旧主任务") == "旧任务已恢复"
        finally:
            await resumed.close()
            await runtime.close()

    asyncio.run(run())
    assert (tmp_path / "legacy.txt").read_text() == "old checkpoint"
    assert len([event for event in events if event["kind"] == "tool.completed"]) == 1
    assert resumed.state["frames"]["main"]["pending"] is None


def test_legacy_child_pending_restores_same_child_and_private_handler_state(tmp_path, monkeypatch):
    child = {
        "id": "child",
        "name": "助手",
        "description": "执行任务",
        "prompt": "LEGACY_CHILD",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    snap = snapshot(subs=[child])

    async def interrupted(model, messages, tools, secret):
        if "LEGACY_CHILD" in messages[0]["content"]:
            raise asyncio.CancelledError()
        return call("delegate_task", {"subagent_id": "child", "task": "恢复子任务"}, "delegate-old")

    monkeypatch.setattr(provider, "chat", interrupted)
    first, _, saved = runner(tmp_path, snap)

    async def interrupt():
        try:
            with pytest.raises(asyncio.CancelledError):
                await first.execute("恢复委派")
        finally:
            await first.close()

    asyncio.run(interrupt())
    legacy = copy.deepcopy(saved[-1])
    pending = legacy["frames"]["main"]["pending"]
    pending["child_instance"] = pending.pop("handler_state")["child_instance"]
    pending.pop("request_id", None)
    assert pending["child_instance"] == "sub-1"
    seen_models = []
    child_actions = [
        call("workspace_write", {"path": "resumed.txt", "content": "same child"}),
        answer("子任务已恢复"),
    ]

    async def resumed_model(model, messages, tools, secret):
        seen_models.append(model["model_id"])
        if not tools:
            return decision()
        if "LEGACY_CHILD" in messages[0]["content"]:
            return child_actions.pop(0)
        return answer("汇总已恢复")

    monkeypatch.setattr(provider, "chat", resumed_model)
    resumed, events, checkpoints = runner(tmp_path, snap, legacy)
    resumed.resume(retry_unknown_models=True)

    async def run():
        try:
            assert await resumed.execute("恢复委派") == "汇总已恢复"
        finally:
            await resumed.close()

    asyncio.run(run())
    assert resumed.state["delegations"] == 1
    assert set(resumed.state["frames"]) == {"main", "sub-1"}
    assert set(seen_models) == {"inherited-model"}
    assert (tmp_path / "subagents/sub-1/resumed.txt").read_text() == "same child"
    assert not any(event["kind"] == "subagent.created" for event in events)
    migrated = [
        state["frames"]["main"]["pending"]
        for state in checkpoints
        if state["frames"]["main"]["pending"]
        and "handler_state" in state["frames"]["main"]["pending"]
    ]
    assert migrated
    assert all("child_instance" not in item for item in migrated)
    assert all(item["handler_state"]["child_instance"] == "sub-1" for item in migrated)


def test_duplicate_tool_name_is_rejected():
    registry = ToolRegistry()

    async def handler(context, args):
        return "original handler"

    definition = ToolDefinition("duplicate", "Example", parameters({}), handler)
    registry.register(definition)
    with pytest.raises(ValueError, match="重复"):
        registry.register(definition)
    assert registry.resolve("duplicate", {}, "main") == definition
    assert registry.resolve("duplicate", {}, "main").handler is definition.handler


def test_tool_context_has_detached_readonly_config_and_no_loop_state(tmp_path):
    config = snapshot()["config"]
    config["subs"] = [{"id": "worker", "tools": ["allowed"]}]
    context = tool_context(tmp_path, [], config)
    config["subs"][0]["tools"].append("later")
    assert context.config["subs"][0]["tools"] == ("allowed",)
    with pytest.raises(TypeError):
        context.config["mode"] = "plan"
    with pytest.raises(TypeError):
        context.config["subs"][0]["id"] = "replacement"
    for name in ("state", "frame", "pending", "persist", "run_child"):
        assert not hasattr(context, name)


def host_context(tmp_path, *, save=None, emit=None, run_child=None, config=None, state=None):
    frame = {"plan": []}
    state = state if state is not None else {"frames": {}, "delegations": 0, "citations": {}}
    pending = {}

    async def no_child(*args):
        raise AssertionError("unexpected child")

    context = build_tool_context(
        config=config or snapshot()["config"],
        frame=frame,
        state=state,
        pending=pending,
        instance="main",
        root_workspace=tmp_path,
        emit=emit or (lambda *args: None),
        persist=save or (lambda: None),
        run_child=run_child or no_child,
    )
    return context, frame, state, pending


def test_capability_writes_are_saved_before_observation_and_detached(tmp_path):
    saves, events = [], []
    context, frame, state, pending = host_context(
        tmp_path,
        save=lambda: saves.append(copy.deepcopy((frame, state, pending))),
        emit=lambda kind, payload: events.append((kind, len(saves))),
    )
    steps = [{"id": "a", "step": "检查", "status": "in_progress"}]
    result = context.plan.update(steps, "开始")
    steps[0]["step"] = "input changed"
    result["steps"][0]["step"] = "result changed"
    assert frame["plan"][0]["step"] == "检查"
    with pytest.raises(TypeError):
        context.plan.read()[0]["step"] = "snapshot changed"
    assert events == [("plan.created", 1), ("step.started", 1)]
    citations_ref = state["citations"]
    context.citations.record([{"id": "doc:1", "file": "a.txt", "content": "raw text"}])
    assert state["citations"] is citations_ref
    assert citations_ref == {"doc:1": {"id": "doc:1", "file": "a.txt"}}
    context.invocation.set("cursor", {"page": 2})
    cursor = context.invocation.get("cursor")
    cursor["page"] = 99
    assert pending["cursor"] == {"page": 2}
    assert len(saves) == 3


@pytest.mark.parametrize("capability", ["plan", "citations", "invocation", "child"])
def test_failed_capability_checkpoint_restores_state_and_propagates(tmp_path, capability):
    events, children = [], []

    def failed_save():
        raise OSError("storage unavailable")

    async def child(*args):
        children.append(args)

    config = snapshot(subs=[{"id": "worker", "name": "Worker"}])["config"]
    context, frame, state, pending = host_context(
        tmp_path,
        save=failed_save,
        emit=lambda *args: events.append(args),
        run_child=child,
        config=config,
    )
    before = copy.deepcopy((frame, state, pending))
    with pytest.raises(ToolPersistenceError):
        if capability == "plan":
            context.plan.update([{"id": "a", "step": "检查", "status": "pending"}], "开始")
        elif capability == "citations":
            context.citations.record([{"id": "doc:1", "content": "raw"}])
        elif capability == "invocation":
            context.invocation.set("cursor", 2)
        else:
            asyncio.run(context.children.run("worker", "do work"))
    assert (frame, state, pending) == before
    assert not events and not children


def test_child_service_reuses_saved_identity_and_enforces_global_limit(tmp_path):
    calls, saves = [], []
    state = {"frames": {}, "delegations": 7, "citations": {}}

    async def run_child(config, task, history, instance):
        calls.append(instance)
        assert saves[-1][1]["child_instance"] == instance
        state["frames"][instance] = {"outcome": "complete"}
        return "done"

    config = snapshot(subs=[{"id": "worker", "name": "Worker"}])["config"]
    context, _, _, pending = host_context(
        tmp_path,
        config=config,
        state=state,
        run_child=run_child,
        save=lambda: saves.append(copy.deepcopy((state, pending))),
    )
    assert asyncio.run(context.children.run("worker", "task")) == {
        "status": "complete",
        "output": "done",
    }
    assert asyncio.run(context.children.run("worker", "task"))["output"] == "done"
    assert calls == ["sub-8", "sub-8"] and state["delegations"] == 8
    other, _, _, _ = host_context(tmp_path, config=config, state=state, run_child=run_child)
    with pytest.raises(ValueError, match="数量限制"):
        asyncio.run(other.children.run("worker", "another task"))


@pytest.mark.parametrize("observer_error", [RuntimeError, asyncio.CancelledError])
def test_notification_failure_or_mutation_does_not_change_tool_result(
    tmp_path, caplog, observer_error
):
    result = {"value": "completed side effect"}
    executions = []

    async def action(context, args):
        executions.append(True)
        return result

    def broken_observer(kind, payload):
        if "result" in payload:
            payload["result"]["value"] = "tampered"
        raise observer_error("secret payload must not be logged")

    context, _, _, _ = host_context(tmp_path, emit=broken_observer)
    registry = ToolRegistry()
    registry.register(ToolDefinition("action", "Action", parameters({}), action))
    runtime = ToolRuntime(snapshot(), registry)

    async def run():
        try:
            outcome = await runtime.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("id", "action", "{}"),
                context=context,
            )
            assert outcome.status == "succeeded"
            assert outcome.value == {"value": "completed side effect"}
        finally:
            await runtime.close()

    asyncio.run(run())
    assert executions == [True]
    assert "secret payload" not in caplog.text
    assert "completed side effect" not in caplog.text


def test_persistence_error_is_not_converted_to_failed_tool_outcome(tmp_path):
    async def action(context, args):
        context.invocation.set("stage", "accepted")

    def failed_save():
        raise OSError("disk failure")

    context, _, _, pending = host_context(tmp_path, save=failed_save)
    registry = ToolRegistry()
    registry.register(ToolDefinition("action", "Action", parameters({}), action))
    runtime = ToolRuntime(snapshot(), registry)

    async def run():
        try:
            with pytest.raises(ToolPersistenceError):
                await runtime.bus.request(
                    TOOL_REQUEST_TOPIC,
                    ToolRequest("id", "action", "{}"),
                    context=context,
                )
        finally:
            await runtime.close()

    asyncio.run(run())
    assert pending == {}


def test_registry_freezes_and_shared_bus_binding_is_released(tmp_path):
    cleaned = []

    async def run():
        bus = EventBus()
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed(context, args):
            try:
                started.set()
                await release.wait()
            finally:
                cleaned.append(True)

        async def other(event):
            return "other capability"

        bus.register("other", other)
        registry = ToolRegistry()
        definition = ToolDefinition("delayed", "Delayed", parameters({}), delayed)
        registry.register(definition)
        runtime = ToolRuntime(snapshot(), registry, bus)
        assert registry.frozen
        with pytest.raises(RuntimeError, match="冻结"):
            registry.register(ToolDefinition("late", "Late", parameters({}), delayed))
        request = asyncio.create_task(
            bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("id", "delayed", "{}"),
                context=tool_context(tmp_path, []),
            )
        )
        await asyncio.wait_for(started.wait(), 1)
        await runtime.close()
        await runtime.close()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert cleaned == [True]
        assert await bus.request("other", None) == "other capability"
        with pytest.raises(UnknownTopicError):
            await bus.request(TOOL_REQUEST_TOPIC, ToolRequest("id2", "delayed", "{}"))
        replacement = ToolRuntime(snapshot(), registry, bus)
        await replacement.close()
        await bus.close()

    asyncio.run(run())


def test_tool_authorization_is_supplied_by_execution_strategy(tmp_path):
    from dataclasses import replace

    called = []

    async def action(context, args):
        called.append(True)
        return "done"

    config = snapshot()["config"]
    config["mode"] = "plan"
    context = tool_context(tmp_path, [], config)
    registry = ToolRegistry()
    registry.register(ToolDefinition("action", "Action", parameters({}), action))
    runtime = ToolRuntime(snapshot(), registry)

    def reject(definition):
        raise ValueError("strategy rejected action")

    async def run():
        try:
            # The tool dispatcher does not impose a concrete Plan strategy.
            result = await runtime.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("first", "action", "{}"),
                context=context,
            )
            assert result.status == "succeeded"
            result = await runtime.bus.request(
                TOOL_REQUEST_TOPIC,
                ToolRequest("second", "action", "{}"),
                context=replace(context, authorize_tool=reject),
            )
            assert result.status == "failed" and "strategy rejected" in result.value["error"]
        finally:
            await runtime.close()

    asyncio.run(run())
    assert called == [True]


def test_builtin_registration_namespace_does_not_version_custom_tools():
    async def handler(context, args):
        return "done"

    registry = ToolRegistry()
    with registry.implementation_namespace("builtin-tools/v1"):
        registry.register(ToolDefinition("builtin", "Builtin", parameters({}), handler))
        registry.register(
            ToolDefinition(
                "explicit",
                "Explicit",
                parameters({}),
                handler,
                implementation_id="custom-explicit/v3",
            )
        )
    registry.register(ToolDefinition("custom", "Custom", parameters({}), handler))
    items = {item["name"]: item for item in registry.bindings()}
    assert list(items) == ["builtin", "custom", "explicit"]
    assert items["builtin"]["implementation_id"] == (
        f"builtin-tools/v1:{handler.__module__}.{handler.__qualname__}"
    )
    assert items["explicit"]["implementation_id"] == "custom-explicit/v3"
    assert items["custom"]["implementation_id"] is None
    with pytest.raises(RuntimeError, match="registration failed"):
        with registry.implementation_namespace("temporary/v2"):
            raise RuntimeError("registration failed")
    registry.register(ToolDefinition("after-error", "After", parameters({}), handler))
    assert registry.bindings()[0]["implementation_id"] is None
    registry.freeze()
    with pytest.raises(RuntimeError, match="冻结"):
        with registry.implementation_namespace("too-late/v1"):
            pass


def test_registry_schema_snapshots_cannot_change_validation_or_checkpoint_manifest():
    async def handler(context, args):
        return args

    schema = parameters({"count": {"type": "integer"}}, ["count"])
    definition = ToolDefinition(
        "count",
        "Count",
        schema,
        handler,
        implementation_id="count/v1",
        before_plan=True,
        resume_inflight=True,
        timeout=12,
        evidence_fields=("count",),
    )
    registry = ToolRegistry()
    registry.register(definition)
    runtime = ToolRuntime(snapshot(), registry)
    expected = copy.deepcopy(runtime.bindings())
    assert expected[0]["before_plan"] is True
    assert expected[0]["resume_inflight"] is True
    assert expected[0]["timeout"] == 12
    assert expected[0]["evidence_fields"] == ["count"]
    schema["properties"]["count"]["type"] = "string"
    exposed = registry.resolve("count", {}, "main")
    assert exposed.handler is handler
    exposed.parameters["properties"]["count"]["type"] = "string"
    registry.definitions({}, "main")[0]["function"]["parameters"]["required"].clear()
    runtime.bindings()[0]["parameters"]["properties"].clear()
    registry.bindings()[0]["evidence_fields"].clear()
    definition.model_schema()["function"]["parameters"]["properties"].clear()
    assert schema["properties"] == {"count": {"type": "string"}}
    assert runtime.bindings() == expected
    registry.validate("count", {"count": 2})
    with pytest.raises(ValueError, match="参数不合法"):
        registry.validate("count", {"count": "changed schema must not be accepted"})
    asyncio.run(runtime.close())


@pytest.mark.parametrize(
    "changed",
    [
        {"implementation_id": "handler/v2"},
        {"timeout": 5},
        {"before_plan": True},
        {"resume_inflight": True},
        {"parameters": parameters({"added": {"type": "string"}})},
    ],
)
def test_registry_manifest_changes_when_executable_contract_changes(changed):
    from dataclasses import replace

    async def handler(context, args):
        return "done"

    definition = ToolDefinition(
        "action", "Action", parameters({}), handler, implementation_id="handler/v1"
    )
    before, after = ToolRegistry(), ToolRegistry()
    before.register(definition)
    after.register(replace(definition, **changed))
    assert before.bindings() != after.bindings()
