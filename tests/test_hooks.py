import asyncio
import copy

import pytest
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookFailed,
    HookManager,
    HookPoint,
    HookPointRegistry,
    HookRecoveryRequired,
    HookRegistry,
    HookRejected,
    PatchInput,
    PatchOutput,
    Reject,
    execute_operation,
)
from agentloom_runtime.tool_contracts import ToolPersistenceError


def manager_for(*entries, total_timeout=10):
    registry = HookRegistry()
    bindings = []
    for definition, binding in entries:
        registry.register(definition)
        bindings.append(binding)
    return HookManager(registry, bindings, total_timeout=total_timeout)


def entry(name, point, handler, *, replay_safe=True, observation=False, **binding):
    return (
        HookDefinition(name, "v1", handler, replay_safe=replay_safe, observation=observation),
        HookBinding(name, name, point, **binding),
    )


def run(coroutine):
    return asyncio.run(coroutine)


def test_order_readonly_config_and_validated_prior_patch():
    seen = []

    async def first(ctx, payload):
        assert ctx.run_id == "run-1"
        assert ctx.operation_id == "op-1"
        with pytest.raises(TypeError):
            payload["arguments"]["limit"] = 7
        with pytest.raises(TypeError):
            ctx.config["nested"]["value"] = 7
        seen.append("first")
        return PatchInput({"arguments": {"limit": 3}})

    async def second(ctx, payload):
        seen.append(payload["arguments"]["limit"])
        return Continue()

    manager = manager_for(
        entry("second", "tool.before", second, priority=20),
        entry("first", "tool.before", first, priority=10, config={"nested": {"value": 1}}),
    )
    original = {"name": "search", "arguments": {"limit": 30}}
    state = {}
    result = run(
        manager.run(
            "tool.before",
            original,
            scope={"run_id": "run-1", "operation_id": "op-1"},
            state=state,
            save=lambda: None,
            validate=lambda data: {**data, "arguments": {"limit": data["arguments"]["limit"] + 1}},
        )
    )
    assert seen == ["first", 4]
    assert result["arguments"]["limit"] == 4
    assert original["arguments"]["limit"] == 30
    assert [record["binding_id"] for record in state["records"]] == ["first", "second"]


def test_equal_priority_uses_published_order_and_duplicate_binding_rejected():
    seen = []

    async def observe(ctx, payload):
        seen.append(ctx.binding_id)
        return Continue()

    registry = HookRegistry()
    registry.register(HookDefinition("same", "v1", observe))
    bindings = [HookBinding("z", "same", "tool.before"), HookBinding("a", "same", "tool.before")]
    manager = HookManager(registry, bindings)
    run(manager.run("tool.before", {}, scope={}, state={}, save=lambda: None))
    assert seen == ["z", "a"]
    with pytest.raises(ValueError, match="unique"):
        HookManager(registry, [bindings[0], bindings[0]])


def test_purpose_target_and_child_filters_and_completion_alias():
    seen = []

    async def observe(ctx, payload):
        seen.append((ctx.binding_id, ctx.purpose, ctx.instance_id))
        return Continue()

    registry = HookRegistry()
    registry.register(HookDefinition("same", "v1", observe))
    manager = HookManager(
        registry,
        [
            HookBinding("default", "same", "model.chat.after"),
            HookBinding(
                "completion",
                "same",
                "model.chat.after",
                purposes=("completion",),
                instances=("child",),
                targets=("m1",),
            ),
        ],
    )
    for scope in (
        {"purpose": "action", "instance_id": "main", "target": "m1"},
        {"purpose": "compaction", "instance_id": "main", "target": "m1"},
        {"purpose": "verification", "instance_id": "sub1", "target": "m1"},
        {"purpose": "verification", "instance_id": "sub2", "target": "m2"},
    ):
        run(manager.run("model.chat.after", {}, scope=scope, state={}, save=lambda: None))
    assert seen == [("default", "action", "main"), ("completion", "completion", "sub1")]


@pytest.mark.parametrize(
    "point,decision",
    [
        ("tool.before", PatchInput({"name": "other"})),
        ("tool.after", PatchOutput({"status": "succeeded"})),
        ("model.chat.after", PatchOutput({"tool_calls": []})),
        ("model.chat.after", Reject("denied", "No")),
        ("tool.before", PatchOutput({"arguments": {}})),
        ("run.before", PatchInput({"task": "different"})),
    ],
)
def test_protected_fields_and_wrong_decisions_fail_closed(point, decision):
    async def bad(ctx, payload):
        return decision

    manager = manager_for(entry("bad", point, bad))
    with pytest.raises(HookFailed):
        run(
            manager.run(
                point,
                {},
                scope={"purpose": "action"},
                state={},
                save=lambda: None,
            )
        )


def test_invalid_patch_stops_before_next_hook_and_action():
    seen = []

    async def bad(ctx, payload):
        return PatchInput({"arguments": {"limit": -1}})

    async def later(ctx, payload):
        seen.append("later")
        return Continue()

    async def action(payload):
        seen.append("action")
        return {"value": "done"}

    def validate(payload):
        assert payload["arguments"]["limit"] >= 0

    manager = manager_for(entry("bad", "tool.before", bad), entry("later", "tool.before", later))
    state = {}
    with pytest.raises(HookFailed):
        run(
            execute_operation(
                manager,
                "tool",
                {"arguments": {"limit": 1}},
                action,
                scope={},
                state=state,
                save=lambda: None,
                validate_input=validate,
            )
        )
    assert seen == []
    assert state["actual_status"] == "not_started"


def test_rejection_is_durable_and_no_real_operation_occurs():
    seen = []

    async def reject(ctx, payload):
        seen.append("hook")
        return Reject("permission_denied", "Task denied")

    async def action(payload):
        seen.append("action")
        return {}

    manager = manager_for(entry("guard", "tool.before", reject))
    state = {}
    for _ in range(2):
        with pytest.raises(HookRejected) as error:
            run(
                execute_operation(
                    manager, "tool", {}, action, scope={}, state=state, save=lambda: None
                )
            )
        assert error.value.code == "permission_denied"
    assert seen == ["hook"]
    assert state["actual_status"] == "not_started"


def test_only_observers_can_fail_open_and_cannot_patch():
    async def observe(ctx, payload):
        return PatchInput({"arguments": {"limit": 0}})

    with pytest.raises(ValueError, match="observation"):
        manager_for(entry("bad", "tool.before", observe, failure_policy="continue"))
    manager = manager_for(
        entry("observer", "tool.before", observe, observation=True, failure_policy="continue")
    )
    state = {}
    original = {"arguments": {"limit": 4}}
    assert (
        run(manager.run("tool.before", original, scope={}, state=state, save=lambda: None))
        == original
    )
    assert state["records"][0]["status"] == "ignored"


def test_independent_and_total_timeout_and_cancellation():
    async def slow(ctx, payload):
        await asyncio.sleep(0.06)
        return Continue()

    manager = manager_for(
        entry("first", "tool.before", slow, timeout=0.5),
        entry("second", "tool.before", slow, timeout=0.5),
        total_timeout=0.09,
    )
    state = {}
    with pytest.raises(HookFailed):
        run(manager.run("tool.before", {}, scope={}, state=state, save=lambda: None))
    assert state["records"][0]["status"] == "completed"
    assert state["records"][1]["error"] == "timeout"

    async def cancelled_case():
        started = asyncio.Event()
        finished = asyncio.Event()

        async def cancellable(ctx, payload):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        current = manager_for(entry("wait", "tool.before", cancellable))
        durable = {}
        task = asyncio.create_task(
            current.run("tool.before", {}, scope={}, state=durable, save=lambda: None)
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert durable["status"] == "cancelled"

    run(cancelled_case())


def test_after_failure_restores_real_result_without_repeating_action_or_completed_hooks():
    calls = []
    fail = True
    checkpoints = []

    async def first(ctx, payload):
        calls.append("first")
        return PatchOutput({"value": "redacted"})

    async def flaky(ctx, payload):
        calls.append("flaky")
        assert payload["value"] == "redacted"
        assert any(
            saved.get("raw_output") == {"value": "private", "status": "succeeded"}
            for saved in checkpoints
        )
        if fail:
            raise ValueError("private exception text must not enter diagnostics")
        return Continue()

    async def action(payload):
        calls.append("action")
        return {"value": "private", "status": "succeeded"}

    manager = manager_for(entry("first", "tool.after", first), entry("flaky", "tool.after", flaky))
    state = {"operation_id": "stable"}

    def execute():
        return execute_operation(
            manager,
            "tool",
            {"arguments": {}},
            action,
            scope={"operation_id": "stable"},
            state=state,
            save=lambda: checkpoints.append(copy.deepcopy(state)),
        )

    with pytest.raises(HookFailed, match="flaky"):
        run(execute())
    assert state["raw_output"]["value"] == "private"
    assert state["actual_status"] == "succeeded"
    assert state["stage"] == "after"
    assert "private exception" not in str(state)
    fail = False
    state = copy.deepcopy(checkpoints[-1])
    assert run(execute())["value"] == "redacted"
    assert run(execute())["value"] == "redacted"
    assert calls == ["action", "first", "flaky", "flaky"]
    assert state["raw_output"]["value"] == "private"
    assert state["effective_output"]["value"] == "redacted"


def test_unsafe_hook_and_unknown_action_require_explicit_reconciliation():
    calls = []

    async def unsafe(ctx, payload):
        calls.append("hook")
        raise RuntimeError("side effect may already have occurred")

    async def action(payload):
        calls.append("action")
        return {"value": "done"}

    manager = manager_for(entry("unsafe", "tool.after", unsafe, replay_safe=False))
    state = {}
    for error in (HookFailed, HookRecoveryRequired):
        with pytest.raises(error):
            run(
                execute_operation(
                    manager, "tool", {}, action, scope={}, state=state, save=lambda: None
                )
            )
    assert calls == ["action", "hook"]

    async def unknown(payload):
        calls.append("unknown")
        raise RuntimeError("disconnected after sending the request")

    empty = HookManager()
    state = {}
    with pytest.raises(RuntimeError):
        run(execute_operation(empty, "tool", {}, unknown, scope={}, state=state, save=lambda: None))
    with pytest.raises(HookRecoveryRequired):
        run(execute_operation(empty, "tool", {}, action, scope={}, state=state, save=lambda: None))
    result = run(
        execute_operation(
            empty,
            "tool",
            {},
            action,
            scope={},
            state=state,
            save=lambda: None,
            resume_inflight=True,
        )
    )
    assert result == {"value": "done"}
    assert calls[-2:] == ["unknown", "action"]


def test_raw_result_is_saved_even_when_output_validation_fails():
    calls = []

    async def action(payload):
        calls.append("action")
        return {"value": "malformed", "status": "succeeded"}

    def invalid(payload):
        raise ValueError("invalid result")

    state = {}
    manager = HookManager()
    for _ in range(2):
        with pytest.raises(ValueError, match="invalid result"):
            run(
                execute_operation(
                    manager,
                    "tool",
                    {},
                    action,
                    scope={},
                    state=state,
                    save=lambda: None,
                    validate_output=invalid,
                )
            )
    assert calls == ["action"]
    assert state["raw_output"]["value"] == "malformed"


def test_diagnostics_never_mask_failure_and_do_not_receive_secrets():
    seen = []

    async def diagnostic(ctx, payload):
        seen.append((ctx.point, dict(payload)))
        raise ValueError("observer failure")

    async def action(payload):
        raise ValueError("actual failure with private body")

    manager = manager_for(
        entry("error", "operation.error", diagnostic, observation=True),
        entry("finally", "operation.finally", diagnostic, observation=True),
    )
    state = {}
    with pytest.raises(ValueError, match="actual failure"):
        run(
            execute_operation(
                manager,
                "tool",
                {"arguments": {"private": "secret"}},
                action,
                scope={},
                state=state,
                save=lambda: None,
            )
        )
    assert [point for point, _ in seen] == ["operation.error", "operation.finally"]
    assert "private" not in str(seen)
    assert "actual failure" not in str(state["attempts"])


def test_snapshot_freezes_registration_and_changed_bindings_reject_recovery():
    async def observe(ctx, payload):
        return Continue()

    config = {"x": 1}
    registry = HookRegistry()
    registry.register(HookDefinition("same", "v1", observe))
    binding = HookBinding("one", "same", "tool.before", config=config)
    manager = HookManager(registry, [binding])
    config["x"] = 2
    assert manager.checkpoint_config()["bindings"][0]["config"]["x"] == 1
    state = {}
    run(manager.run("tool.before", {}, scope={}, state=state, save=lambda: None))
    different = HookManager(registry, [binding])
    with pytest.raises(HookRecoveryRequired):
        run(different.run("tool.before", {}, scope={}, state=state, save=lambda: None))


def test_schema_finite_json_async_handlers_and_custom_point():
    registry = HookRegistry()
    with pytest.raises(ValueError, match="async"):
        registry.register(HookDefinition("sync", "v1", lambda ctx, payload: Continue()))

    async def observe(ctx, payload):
        return Continue()

    registry.register(
        HookDefinition(
            "validated", "v1", observe, config_schema={"type": "object", "required": ["x"]}
        )
    )
    with pytest.raises(ValueError, match="schema"):
        HookManager(registry, [HookBinding("invalid", "validated", "tool.before")])
    with pytest.raises(ValueError, match="observation"):
        HookManager(
            registry, [HookBinding("invalid", "validated", "operation.error", config={"x": 1})]
        )
    manager = HookManager()
    with pytest.raises(ValueError, match="finite"):
        run(manager.run("tool.before", {"x": float("nan")}, scope={}, state={}, save=lambda: None))
    with pytest.raises(ValueError, match="identity"):
        run(manager.run("tool.before", {}, scope={"secret": "key"}, state={}, save=lambda: None))
    points = HookPointRegistry()
    points.register(HookPoint("custom.before", "before", ("text",), schema={"type": "object"}))
    manager = HookManager(points=points)
    assert run(
        manager.run("custom.before", {"text": "ok"}, scope={}, state={}, save=lambda: None)
    ) == {"text": "ok"}


def test_checkpoint_failure_stops_dispatch():
    calls = []

    async def action(payload):
        calls.append("action")
        return {}

    def fail_save():
        raise OSError("private database configuration")

    with pytest.raises(ToolPersistenceError, match="persisted"):
        run(
            execute_operation(HookManager(), "tool", {}, action, scope={}, state={}, save=fail_save)
        )
    assert calls == []


def test_preparation_resumes_without_repeating_input_hooks():
    calls = []
    fail = True

    async def before(ctx, payload):
        calls.append("hook")
        return PatchInput({"messages": [{"role": "user", "content": "hook-added"}]})

    async def prepare(payload):
        calls.append("prepare")
        if fail:
            raise RuntimeError("nested compaction failed")
        return {**payload, "messages": [{"role": "user", "content": "compressed"}]}

    async def action(payload):
        calls.append(payload["messages"][0]["content"])
        return {"content": "done"}

    manager = manager_for(entry("before", "model.chat.before", before))
    state = {}

    def execute():
        return execute_operation(
            manager,
            "model.chat",
            {"messages": []},
            action,
            scope={"purpose": "action"},
            state=state,
            save=lambda: None,
            prepare_input=prepare,
        )

    with pytest.raises(RuntimeError):
        run(execute())
    assert state["actual_status"] == "not_started"
    assert state["stage"] == "preparing"
    fail = False
    assert run(execute()) == {"content": "done"}
    assert calls == ["hook", "prepare", "prepare", "compressed"]


def test_code_change_under_same_version_is_detected_at_recovery():
    async def original(ctx, payload):
        return Continue()

    async def changed(ctx, payload):
        return PatchInput({"arguments": {"limit": 10}})

    before = manager_for(entry("same", "tool.before", original))
    after = manager_for(entry("same", "tool.before", changed))
    assert before.checkpoint_config()["bindings"][0]["code_hash"].startswith("sha256:")
    assert before.checkpoint_config() != after.checkpoint_config()
    state = {}
    run(before.run("tool.before", {}, scope={}, state=state, save=lambda: None))
    with pytest.raises(HookRecoveryRequired):
        run(after.run("tool.before", {}, scope={}, state=state, save=lambda: None))


def test_cancelled_actual_action_stays_unknown_and_is_not_repeated():
    async def case():
        started = asyncio.Event()
        calls = []
        checkpoints = []

        async def action(payload):
            calls.append("action")
            started.set()
            await asyncio.Event().wait()

        async def cleanup(ctx, payload):
            calls.append("cleanup")
            return Continue()

        manager = manager_for(entry("cleanup", "operation.finally", cleanup, observation=True))
        state = {}

        def execute():
            return execute_operation(
                manager,
                "tool",
                {},
                action,
                scope={},
                state=state,
                save=lambda: checkpoints.append(copy.deepcopy(state)),
            )

        task = asyncio.create_task(execute())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert checkpoints[-1]["actual_status"] == "unknown"
        assert state["operation_id"] == state["scope"]["operation_id"]
        with pytest.raises(HookRecoveryRequired):
            await execute()
        assert calls.count("action") == 1
        assert calls.count("cleanup") == 2

    run(case())


def test_typed_point_schema_rejects_invalid_field_shapes():
    manager = HookManager()
    for point, payload in (
        ("tool.before", {"arguments": "not-an-object"}),
        ("model.chat.after", {"content": ["not-a-string"]}),
        ("context.prepare.before", {"additional_messages": "not-an-array"}),
        ("model.chat.before", {"temperature": -1}),
    ):
        with pytest.raises(HookFailed, match="schema"):
            run(manager.run(point, payload, scope={}, state={}, save=lambda: None))


def test_async_bound_handler_keeps_its_registered_owner():
    class Counter:
        def __init__(self):
            self.calls = 0

        async def hook(self, ctx, payload):
            self.calls += 1
            return Continue()

    counter = Counter()
    manager = manager_for(entry("bound", "tool.before", counter.hook))
    run(manager.run("tool.before", {}, scope={}, state={}, save=lambda: None))
    assert counter.calls == 1


def test_blocked_lifecycle_is_valid_with_no_custom_hooks():
    from agentloom_runtime.lifecycle_hooks import RuntimeLifecycle

    async def action():
        return "Need the repository location"

    record = {"operation_id": "run-lifecycle"}
    result = run(
        RuntimeLifecycle(HookManager()).run(
            "run",
            "main",
            "review code",
            action,
            record=record,
            save=lambda: None,
            outcome=lambda: "blocked",
        )
    )
    assert result == "Need the repository location"
    assert record["operation"]["raw_output"]["status"] == "blocked"
    assert record["completed"] is True


def test_cached_final_input_is_revalidated_before_actual_dispatch():
    calls = []
    permitted = True
    interrupt_once = True
    state = {}

    async def before(ctx, payload):
        calls.append("hook")
        return Continue()

    async def action(payload):
        calls.append("action")
        return {"value": "done"}

    def final_guard(payload):
        calls.append("guard")
        if not permitted:
            raise ValueError("budget no longer permits dispatch")

    def save():
        nonlocal interrupt_once
        if "final_input" in state and state["actual_status"] == "not_started" and interrupt_once:
            interrupt_once = False
            raise ToolPersistenceError("interrupted after preparation")

    manager = manager_for(entry("before", "tool.before", before))

    def execute():
        return execute_operation(
            manager,
            "tool",
            {},
            action,
            scope={},
            state=state,
            save=save,
            validate_prepared=final_guard,
        )

    with pytest.raises(ToolPersistenceError):
        run(execute())
    permitted = False
    with pytest.raises(ValueError, match="budget no longer"):
        run(execute())
    assert calls == ["hook", "guard", "guard"]
    assert state["actual_status"] == "not_started"
