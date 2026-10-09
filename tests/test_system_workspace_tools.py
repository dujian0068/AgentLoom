"""Real bus/hook boundaries around replaceable, tenant-scoped file capabilities."""

import asyncio
import copy
import hashlib
import json

import pytest
from agentloom_runtime.execution_services import build_tool_context
from agentloom_runtime.handlers import register_builtins
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookError,
    HookManager,
    HookRegistry,
    PatchInput,
    PatchOutput,
    Reject,
)
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import TOOL_REQUEST_TOPIC, ToolRequest
from agentloom_runtime.tool_runtime import ToolRegistry
from agentloom_runtime.workspace import WorkspaceProvider
from test_harness import answer, call, snapshot
from test_runtime_modules import Accept, FakeGateway


def scoped_provider(tmp_path, *, run="run-1", factory=None, binding=None):
    return WorkspaceProvider(
        main_root=tmp_path / "session/main",
        children_root=tmp_path / "session/runs" / run / "children",
        lock_root=tmp_path / ".locks",
        store_factory=factory,
        binding=binding
        or {
            "implementation": "session-workspace/v1",
            "backend": "shared_posix",
            "volume_id": "test-volume",
            "space_id": "space-a",
            "agent_id": "agent-a",
            "session_id": "session-a",
            "run_id": run,
        },
    )


def make(tmp_path, workspace_provider, *, hooks=None, gateway=None, **options):
    return create_engine(
        snapshot(),
        tmp_path / "legacy-ignored",
        lambda *a: None,
        lambda x: x,
        None,
        workspace_provider=workspace_provider,
        hooks=hooks,
        model_gateway=gateway or FakeGateway(),
        completion_policy=Accept(),
        **options,
    )


def context(tmp_path, *, instance="main", pending=None, saves=None):
    pending = pending if pending is not None else {}

    async def no_child(*args):
        raise AssertionError("unexpected child")

    return build_tool_context(
        config=snapshot()["config"],
        frame={"plan": []},
        state={"frames": {}, "delegations": 0, "citations": {}},
        pending=pending,
        instance=instance,
        root_workspace=tmp_path / "untrusted-context-path",
        emit=lambda *a: None,
        persist=lambda: saves.append(copy.deepcopy(pending)) if saves is not None else None,
        run_child=no_child,
    )


async def request(
    runtime,
    tmp_path,
    name,
    arguments,
    *,
    instance="main",
    pending=None,
    saves=None,
    request_id="request-1",
    uncertain=False,
):
    return await runtime.tools.bus.request(
        TOOL_REQUEST_TOPIC,
        ToolRequest(
            "call-" + request_id, "workspace_" + name, json.dumps(arguments), uncertain=uncertain
        ),
        context=context(tmp_path, instance=instance, pending=pending, saves=saves),
        correlation_id=request_id,
    )


def hooks(*items):
    registry, bindings = HookRegistry(), []
    for index, (point, handler) in enumerate(items):
        identity = "workspace-test-" + str(index)
        registry.register(HookDefinition(identity, "v1", handler, replay_safe=True))
        bindings.append(HookBinding(identity, identity, point, targets=("workspace_write",)))
    return HookManager(registry, bindings)


def test_file_capabilities_run_through_correlated_bus_and_scope(tmp_path):
    runtime = make(tmp_path, scoped_provider(tmp_path))
    observations = []

    async def observer(event):
        observations.append((event.topic, event.correlation_id))

    runtime.tools.bus.subscribe("request.started", observer)
    runtime.tools.bus.subscribe("request.completed", observer)

    async def run():
        number = 0

        async def perform(name, args, *, succeed=True):
            nonlocal number
            number += 1
            result = await request(runtime, tmp_path, name, args, request_id=str(number))
            assert result.status == ("succeeded" if succeed else "failed"), result.value
            return result.value

        try:
            await perform("mkdir", {"path": "reports"})
            original = await perform(
                "write",
                {"path": "reports/a.md", "content": "alpha\nbeta\nalpha\n", "expected_sha256": ""},
            )
            value = await perform("read", {"path": "reports/a.md", "offset": 2, "limit": 1})
            assert value["content"] == "beta\n" and value["truncated"]
            assert value["sha256"] == original["sha256"]
            assert (await perform("list", {"path": "reports"}))["entries"] == [
                {"path": "reports/a.md", "type": "file", "size": original["size"]}
            ]
            assert (await perform("glob", {"pattern": "**/*.md"}))["paths"] == ["reports/a.md"]
            assert [
                value["line"]
                for value in (await perform("grep", {"pattern": "^alpha$", "literal": False}))[
                    "matches"
                ]
            ] == [1, 3]
            assert (
                "ambiguous"
                in (
                    await perform(
                        "edit",
                        {"path": "reports/a.md", "old_text": "alpha", "new_text": "gamma"},
                        succeed=False,
                    )
                )["error"]
            )
            edited = await perform(
                "edit",
                {
                    "path": "reports/a.md",
                    "old_text": "alpha",
                    "new_text": "gamma",
                    "replace_all": True,
                    "expected_sha256": original["sha256"],
                },
            )
            assert edited["sha256"] != original["sha256"]
            assert (
                "precondition"
                in (
                    await perform(
                        "write",
                        {
                            "path": "reports/a.md",
                            "content": "stale",
                            "expected_sha256": original["sha256"],
                        },
                        succeed=False,
                    )
                )["error"]
            )
            assert (await perform("stat", {"path": "reports/a.md"}))["sha256"] == edited["sha256"]
            await perform("delete", {"path": "reports/a.md", "expected_sha256": edited["sha256"]})
            assert (await perform("list", {"path": "reports"}))["entries"] == []
        finally:
            await runtime.close()
        assert runtime.tools.bus.pending_count == 0
        assert observations == [
            item
            for index in range(1, number + 1)
            for item in (("request.started", str(index)), ("request.completed", str(index)))
        ]

    asyncio.run(run())
    assert not (tmp_path / "untrusted-context-path").exists()


@pytest.mark.parametrize(
    "name,args",
    [
        ("list", {}),
        ("stat", {"path": "file"}),
        ("glob", {"pattern": "**/*"}),
        ("grep", {"pattern": "private"}),
        ("read", {"path": "file"}),
        ("write", {"path": "file", "content": "x"}),
        ("edit", {"path": "file", "old_text": "x", "new_text": "y"}),
        ("mkdir", {"path": "directory"}),
        ("delete", {"path": "file"}),
        ("command", {"command": "pwd"}),
    ],
)
def test_schemas_reject_model_supplied_storage_or_tenant_identity(tmp_path, name, args):
    opened = []

    def forbidden_factory(*args, **kwargs):
        opened.append(True)
        raise AssertionError("storage reached before schema validation")

    runtime = make(tmp_path, scoped_provider(tmp_path, factory=forbidden_factory))

    async def run():
        try:
            for key in (
                "root",
                "space_id",
                "agent_id",
                "session_id",
                "instance",
                "lock_root",
                "volume_id",
            ):
                result = await request(runtime, tmp_path, name, {**args, key: "different-tenant"})
                assert result.status == "failed" and "参数不合法" in result.value["error"]
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not opened


def test_main_and_child_tools_cannot_access_other_instance_files(tmp_path):
    storage = scoped_provider(tmp_path)
    runtime = make(tmp_path, storage)

    async def run():
        try:
            for instance, content in (
                ("main", "MAIN-PRIVATE"),
                ("sub-1", "FIRST-CHILD"),
                ("sub-2", "SECOND-CHILD"),
            ):
                assert (
                    await request(
                        runtime,
                        tmp_path,
                        "write",
                        {"path": "same.txt", "content": content},
                        instance=instance,
                    )
                ).status == "succeeded"
                assert (
                    await request(
                        runtime, tmp_path, "read", {"path": "same.txt"}, instance=instance
                    )
                ).value["content"] == content
            for instance, path in (
                ("main", "subagents/sub-1/same.txt"),
                ("main", "../runs/run-1/children/sub-1/same.txt"),
                ("sub-1", "../sub-2/same.txt"),
                ("sub-1", "../../../main/same.txt"),
                ("sub-2", str(storage.main_root / "same.txt")),
            ):
                result = await request(runtime, tmp_path, "read", {"path": path}, instance=instance)
                assert result.status == "failed"
                assert not any(
                    secret in json.dumps(result.value)
                    for secret in ("MAIN-PRIVATE", "FIRST-CHILD", "SECOND-CHILD")
                )
            for instance in ("main", "sub-1", "sub-2"):
                assert (
                    await request(
                        runtime,
                        tmp_path,
                        "glob",
                        {"pattern": "**/*", "include_hidden": True},
                        instance=instance,
                    )
                ).value["paths"] == ["same.txt"]
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not (storage.main_root / "subagents").exists()


def test_logical_namespace_locks_are_stable_across_mounts_and_questions(tmp_path):
    first, later, other = (
        scoped_provider(tmp_path / node, run=run)
        for node, run in (("a", "first"), ("a", "second"), ("b", "first"))
    )
    a, b, c = (value.for_instance("main") for value in (first, later, other))
    assert a.root == b.root and a.root != c.root
    assert a._lock_name == b._lock_name == c._lock_name
    assert first.for_instance("sub-1")._lock_name == other.for_instance("sub-1")._lock_name
    assert first.for_instance("sub-1")._lock_name != later.for_instance("sub-1")._lock_name
    changed = {**first.binding, "agent_id": "another-agent"}
    assert (
        a._lock_name
        != scoped_provider(tmp_path / "other-app", binding=changed).for_instance("main")._lock_name
    )
    assert first.checkpoint_binding() == other.checkpoint_binding()


def test_before_and_after_hooks_wrap_actual_storage_and_persist_original_result(tmp_path):
    storage, seen, pending, saves = scoped_provider(tmp_path), [], {}, []

    async def before(ctx, data):
        seen.append((ctx.point, ctx.tool_id, ctx.instance_id))
        return PatchInput({"arguments": {**data["arguments"], "content": "redacted"}})

    async def after(ctx, data):
        seen.append((ctx.point, ctx.tool_id, ctx.instance_id))
        raw = pending["__tool_runtime_operation"]["raw_output"]["value"]
        assert raw["sha256"] == hashlib.sha256(b"redacted").hexdigest()
        assert saves[-1]["__tool_runtime_operation"]["raw_output"]["value"] == raw
        assert (storage.main_root / "report.txt").read_text() == "redacted"
        return PatchOutput({"value": {"summary": "stored with policy"}})

    runtime = make(tmp_path, storage, hooks=hooks(("tool.before", before), ("tool.after", after)))

    async def run():
        try:
            result = await request(
                runtime,
                tmp_path,
                "write",
                {"path": "report.txt", "content": "sensitive"},
                pending=pending,
                saves=saves,
            )
            assert result.status == "succeeded" and result.value == {
                "summary": "stored with policy"
            }
            assert result.raw_value["path"] == "report.txt" and result.evidence_arguments == {
                "path": "report.txt"
            }
        finally:
            await runtime.close()

    asyncio.run(run())
    assert seen == [
        ("tool.before", "workspace_write", "main"),
        ("tool.after", "workspace_write", "main"),
    ]


def test_rejecting_before_hook_causes_no_filesystem_write(tmp_path):
    storage = scoped_provider(tmp_path)

    async def reject(ctx, data):
        return Reject("policy", "write denied")

    runtime = make(tmp_path, storage, hooks=hooks(("tool.before", reject)))

    async def run():
        try:
            result = await request(
                runtime, tmp_path, "write", {"path": "never.txt", "content": "must not exist"}
            )
            assert result.status == "failed" and result.value["code"] == "hook_rejected"
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not storage.main_root.exists() and not storage.lock_root.exists()


def test_after_hook_failure_resumes_saved_write_without_repeating_effect(tmp_path):
    pending, calls = {}, []

    async def after(ctx, data):
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("temporary observer failure")
        return Continue()

    runtime = make(tmp_path, scoped_provider(tmp_path), hooks=hooks(("tool.after", after)))

    async def run():
        args = {"path": "once", "content": "one", "expected_sha256": ""}
        try:
            with pytest.raises(HookError):
                await request(runtime, tmp_path, "write", args, pending=pending)
            result = await request(
                runtime, tmp_path, "write", args, pending=pending, uncertain=True
            )
            assert result.status == "succeeded" and result.value["path"] == "once"
        finally:
            await runtime.close()

    asyncio.run(run())
    assert len(calls) == 2


def test_loop_accepts_replaced_storage_without_local_filesystem_implementation(tmp_path):
    calls, contents = [], {}

    class MemoryStore:
        def __init__(self, root, **options):
            self.root, self.namespace = root, options["lock_key"]

        def write(self, path, content):
            calls.append(("write", path, self.namespace))
            contents[(self.namespace, path)] = content
            return {"file": path, "path": path, "size": len(content)}

        def read(self, path):
            calls.append(("read", path, self.namespace))
            return {"path": path, "content": contents[(self.namespace, path)]}

    storage = scoped_provider(tmp_path, factory=MemoryStore)
    runtime = make(
        tmp_path,
        storage,
        gateway=FakeGateway(
            [
                call(
                    "workspace_write", {"path": "memory.md", "content": "backend supplied"}, "write"
                ),
                call("workspace_read", {"path": "memory.md"}, "read"),
                answer("done"),
            ]
        ),
    )

    async def run():
        try:
            assert await runtime.execute("write and read") == "done"
        finally:
            await runtime.close()

    asyncio.run(run())
    assert [item[0] for item in calls] == ["write", "read"] and len(
        {item[2] for item in calls}
    ) == 1
    results = [
        json.loads(message["content"])
        for message in runtime.state["frames"]["main"]["messages"]
        if message["role"] == "tool"
    ]
    assert results[1]["content"] == "backend supplied"
    assert not storage.main_root.exists() and not storage.lock_root.exists()


def test_legacy_checkpoint_preserves_original_catalog_and_read_result(tmp_path):
    snap, registry = snapshot(), ToolRegistry()
    with registry.implementation_namespace("builtin-tools/v1"):
        register_builtins(registry, snap, decrypt=lambda x: x, search=None, workspace_provider=None)
    original = create_engine(
        snap,
        tmp_path / "old-run",
        lambda *a: None,
        lambda x: x,
        None,
        registry=registry,
        model_gateway=FakeGateway(
            [call("workspace_write", {"path": "old.txt", "content": "legacy"}), answer("done")]
        ),
        completion_policy=Accept(),
    )

    async def first():
        try:
            await original.execute("write")
        finally:
            await original.close()

    asyncio.run(first())
    state = copy.deepcopy(original.state)
    assert state["frames"]["main"]["messages"] and "workspace_binding" not in state
    old_catalog = copy.deepcopy(state["modules"]["tools"])
    assert "workspace_list" not in {item["name"] for item in old_catalog}
    restored = create_engine(
        snap,
        tmp_path / "old-run",
        lambda *a: None,
        lambda x: x,
        None,
        checkpoint=state,
        model_gateway=FakeGateway(
            [call("workspace_read", {"path": "old.txt"}), answer("read complete")]
        ),
        completion_policy=Accept(),
    )

    async def second():
        try:
            restored.resume("read the file")
            assert await restored.execute("write") == "read complete"
        finally:
            await restored.close()

    asyncio.run(second())
    assert (
        restored.state["modules"]["tools"] == old_catalog
        and "workspace_binding" not in restored.state
    )
    results = [
        message
        for message in restored.state["frames"]["main"]["messages"]
        if message["role"] == "tool"
    ]
    assert json.loads(results[-1]["content"]) == "legacy"


def test_new_checkpoint_refuses_different_workspace_identity(tmp_path):
    storage = scoped_provider(tmp_path)
    runtime = make(tmp_path, storage)
    state = copy.deepcopy(runtime.state)
    asyncio.run(runtime.close())
    changed = {**storage.binding, "session_id": "another-session"}
    with pytest.raises(ValueError, match="工作区身份"):
        make(tmp_path, scoped_provider(tmp_path, binding=changed), checkpoint=state)


def test_unknown_write_is_not_replayed_but_read_can_be_retried(tmp_path):
    storage = scoped_provider(tmp_path)
    storage.for_instance("main").write("existing", "actual outcome")
    runtime = make(tmp_path, storage)

    async def run():
        try:
            denied = await request(
                runtime,
                tmp_path,
                "write",
                {"path": "unknown", "content": "duplicate"},
                uncertain=True,
            )
            assert denied.status == "failed" and "结果未知" in denied.value["error"]
            observed = await request(
                runtime, tmp_path, "read", {"path": "existing"}, uncertain=True
            )
            assert observed.status == "succeeded"
            assert observed.value["content"] == "actual outcome"
        finally:
            await runtime.close()

    asyncio.run(run())
    assert not (storage.main_root / "unknown").exists()
