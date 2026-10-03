"""Tool requests cross the bus without exposing implementations to the agent loop."""

import asyncio
import copy
import json

import pytest
from agentloom_runtime import engine, mcp_tools
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import (
    TOOL_REQUEST_TOPIC,
    ToolContext,
    ToolDefinition,
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

    return ToolContext(
        config=config or snapshot()["config"],
        frame={"plan": []},
        state={"frames": {}, "delegations": 0},
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

    monkeypatch.setattr(engine, "chat", fake_model)
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

    monkeypatch.setattr(engine, "chat", fake_model)
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

    monkeypatch.setattr(engine, "chat", fake_model)
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

    monkeypatch.setattr(engine, "chat", interrupted)
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

    monkeypatch.setattr(engine, "chat", resumed_model)
    resumed, events, checkpoints = runner(tmp_path, snap, legacy)
    resumed.resume()

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
    assert registry.resolve("duplicate", {}, "main") is definition
