"""Hook integration uses deterministic gateways and durable checkpoint copies."""

import asyncio
import copy

import pytest
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookFailed,
    HookManager,
    HookRecoveryRequired,
    HookRegistry,
    PatchInput,
    PatchOutput,
)
from agentloom_runtime.module_contracts import CompactionResult
from test_harness import answer, call
from test_runtime_modules import FakeGateway, NoCompaction, make


def manager(*entries):
    registry = HookRegistry()
    bindings = []
    for index, entry in enumerate(entries):
        point, handler, *options = entry
        settings = options[0] if options else {}
        identifier = f"hook-{index}"
        registry.register(HookDefinition(identifier, "v1", handler, replay_safe=True))
        bindings.append(HookBinding(identifier, identifier, point, **settings))
    return HookManager(registry, bindings)


def run(coroutine):
    return asyncio.run(coroutine)


def test_model_input_output_scope_and_raw_history_are_separate(tmp_path):
    scopes = []

    async def before(ctx, payload):
        scopes.append(dict(ctx.scope))
        # Frozen nested data cannot be used to rewrite runtime state.
        with pytest.raises(TypeError):
            payload["messages"][0]["content"] = "overwritten"
        messages = [dict(item) for item in payload["messages"]]
        return PatchInput(
            {"messages": messages + [{"role": "user", "content": "temporary"}], "temperature": 0.2}
        )

    async def after(ctx, payload):
        assert payload["usage"]["total_tokens"] == 7
        return PatchOutput({"content": payload["content"].strip()})

    gateway = FakeGateway([{**answer("  finished  "), "usage": {"total_tokens": 7}}])
    runtime = make(
        tmp_path,
        model_gateway=gateway,
        hooks=manager(("model.chat.before", before), ("model.chat.after", after)),
        hook_scope={"space_id": "s", "actor_id": "u", "run_id": "r", "published_revision": 3},
    )
    assert run(runtime.execute("task")) == "finished"
    frame = runtime.state["frames"]["main"]
    assert frame["context"]["records"][-1]["message"]["content"] == "  finished  "
    assert frame["messages"][-1]["content"] == "finished"
    assert not any(message.get("content") == "temporary" for message in frame["messages"])
    assert gateway.requests[0].options == {"temperature": 0.2}
    assert gateway.requests[0].messages[-1]["content"] == "temporary"
    assert scopes[0]["run_id"] == "r" and scopes[0]["published_revision"] == 3
    assert "secret" not in scopes[0]
    operation = next(iter(runtime.state["operations"].values()))
    assert operation["raw_output"]["usage"] == {"total_tokens": 7}
    assert operation["effective_output"]["content"] == "finished"
    run(runtime.close())


def test_after_failure_recovers_only_unfinished_hooks_without_model_charge(tmp_path):
    counts = {"before": 0, "trim": 0, "last": 0}

    async def before(ctx, value):
        counts["before"] += 1
        return Continue()

    async def trim(ctx, value):
        counts["trim"] += 1
        return PatchOutput({"content": value["content"].strip()})

    async def last(ctx, value):
        counts["last"] += 1
        if counts["last"] == 1:
            raise RuntimeError("temporary processing outage")
        assert value["content"] == "done"
        return Continue()

    entries = (
        ("model.chat.before", before),
        ("model.chat.after", trim),
        ("model.chat.after", last),
    )
    saved = []
    first_gateway = FakeGateway([answer("  done  ")])
    runtime = make(
        tmp_path,
        model_gateway=first_gateway,
        hooks=manager(*entries),
        save=lambda value: saved.append(copy.deepcopy(value)),
    )
    with pytest.raises(HookFailed):
        run(runtime.execute("task"))
    checkpoint = saved[-1]
    assert next(iter(checkpoint["operations"].values()))["actual_status"] == "succeeded"
    assert not any(
        record["kind"] == "model" for record in checkpoint["frames"]["main"]["context"]["records"]
    )
    run(runtime.close())
    second_gateway = FakeGateway()
    resumed = make(
        tmp_path, model_gateway=second_gateway, hooks=manager(*entries), checkpoint=checkpoint
    )
    resumed.resume()
    assert run(resumed.execute("ignored")) == "done"
    assert len(first_gateway.requests) == 1 and not second_gateway.requests
    assert counts == {"before": 1, "trim": 1, "last": 2}
    run(resumed.close())


def test_supplement_waits_for_saved_response_then_reaches_next_model(tmp_path):
    attempts = 0

    async def after(ctx, value):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("temporary")
        return Continue()

    entries = (("model.chat.after", after),)
    runtime = make(
        tmp_path, model_gateway=FakeGateway([answer("old answer")]), hooks=manager(*entries)
    )
    with pytest.raises(HookFailed):
        run(runtime.execute("task"))
    checkpoint = copy.deepcopy(runtime.state)
    run(runtime.close())
    gateway = FakeGateway([answer("new answer")])
    resumed = make(tmp_path, model_gateway=gateway, hooks=manager(*entries), checkpoint=checkpoint)
    resumed.resume("also explain the files")
    assert run(resumed.execute("task")) == "new answer"
    assert len(gateway.requests) == 1
    assert any(
        "also explain the files" in (message.get("content") or "")
        for message in gateway.requests[0].messages
    )
    records = resumed.state["frames"]["main"]["context"]["records"]
    assert [row["message"]["content"] for row in records if row["kind"] == "model"] == [
        "old answer",
        "new answer",
    ]
    run(resumed.close())


@pytest.mark.parametrize(
    "patch",
    [
        {"tools": []},
        {"messages": [{"role": "system", "content": "replace"}]},
        {"max_tokens": 999999},
        {"temperature": True},
    ],
)
def test_model_before_cannot_override_protected_input(tmp_path, patch):
    async def before(ctx, value):
        return PatchInput(patch)

    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, hooks=manager(("model.chat.before", before)))
    with pytest.raises(HookFailed):
        run(runtime.execute("task"))
    assert not gateway.requests
    run(runtime.close())


@pytest.mark.parametrize(
    "patch", [{"tool_calls": []}, {"usage": {"total_tokens": 0}}, {"content": ""}]
)
def test_model_after_cannot_change_protocol_or_real_usage(tmp_path, patch):
    async def after(ctx, value):
        return PatchOutput(patch)

    runtime = make(tmp_path, hooks=manager(("model.chat.after", after)))
    with pytest.raises(HookFailed):
        run(runtime.execute("task"))
    operation = next(iter(runtime.state["operations"].values()))
    assert operation["raw_output"]["content"] == "done"
    assert operation["actual_status"] == "succeeded"
    run(runtime.close())


def test_model_before_reprepares_once_without_repeating_hook(tmp_path):
    visits = []

    class Policy(NoCompaction):
        module_id = "request-compaction-test/v1"

        async def compact(self, context, model):
            visits.append(copy.deepcopy(context.messages))
            if context.messages[-1]["content"] == "oversized temporary input":
                return CompactionResult(
                    [*context.messages[:-1], {"role": "user", "content": "summary"}]
                )
            return None

    before_count = 0

    async def before(ctx, value):
        nonlocal before_count
        before_count += 1
        return PatchInput(
            {
                "messages": [
                    *[dict(item) for item in value["messages"]],
                    {"role": "user", "content": "oversized temporary input"},
                ]
            }
        )

    gateway = FakeGateway()
    runtime = make(
        tmp_path,
        model_gateway=gateway,
        compaction_policy=Policy(),
        hooks=manager(("model.chat.before", before)),
    )
    assert run(runtime.execute("task")) == "done"
    assert before_count == 1 and len(visits) == 2
    assert gateway.requests[0].messages[-1]["content"] == "summary"
    assert all(
        row.get("content") != "summary" for row in runtime.state["frames"]["main"]["messages"]
    )
    run(runtime.close())


def test_default_model_hook_skips_verification_and_explicit_alias_matches(tmp_path):
    from agentloom_runtime.module_contracts import CompletionDecision, ModelRequest

    purposes = []

    async def action_hook(ctx, value):
        purposes.append(("action", ctx.purpose))
        return Continue()

    async def completion_hook(ctx, value):
        purposes.append(("review", ctx.purpose))
        return Continue()

    class Reviewer:
        module_id = "hook-reviewer/v1"

        async def review(self, context, model):
            await model(ModelRequest([{"role": "user", "content": "review"}], []))
            return CompletionDecision("complete", "checked")

    runtime = make(
        tmp_path,
        completion_policy=Reviewer(),
        hooks=manager(
            ("model.chat.before", action_hook),
            ("model.chat.before", completion_hook, {"purposes": ("completion",)}),
        ),
    )
    run(runtime.execute("task"))
    assert purposes == [("action", "action"), ("review", "completion")]
    run(runtime.close())


def test_child_inherits_same_chain_with_distinct_scope(tmp_path):
    from agentloom_runtime.runtime import create_engine
    from test_harness import snapshot
    from test_runtime_modules import Accept

    visits = []

    async def child_hook(ctx, value):
        visits.append((ctx.point, ctx.instance_id))
        return Continue()

    child = {
        "id": "helper",
        "name": "helper",
        "description": "help",
        "prompt": "child",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    gateway = FakeGateway(
        [
            call("delegate_task", {"subagent_id": "helper", "task": "help"}),
            answer("child done"),
            answer("parent done"),
        ]
    )
    runtime = create_engine(
        snapshot(subs=[child]),
        tmp_path,
        lambda *a: None,
        lambda value: value,
        None,
        model_gateway=gateway,
        completion_policy=Accept(),
        compaction_policy=NoCompaction(),
        hooks=manager(
            ("subagent.before", child_hook),
            ("model.chat.before", child_hook, {"instances": ("child",)}),
            ("subagent.after", child_hook),
        ),
    )
    assert run(runtime.execute("task")) == "parent done"
    assert visits == [
        ("subagent.before", "sub-1"),
        ("model.chat.before", "sub-1"),
        ("subagent.after", "sub-1"),
    ]
    run(runtime.close())


def test_known_response_not_retried_even_with_explicit_unknown_retry(tmp_path):
    async def after(ctx, value):
        raise ValueError("must repair hook")

    entries = (("model.chat.after", after),)
    runtime = make(tmp_path, hooks=manager(*entries))
    with pytest.raises(HookFailed):
        run(runtime.execute("task"))
    checkpoint = copy.deepcopy(runtime.state)
    run(runtime.close())
    gateway = FakeGateway()
    resumed = make(tmp_path, model_gateway=gateway, hooks=manager(*entries), checkpoint=checkpoint)
    resumed.resume(retry_unknown_models=True)
    with pytest.raises(HookFailed):
        run(resumed.execute("task"))
    assert not gateway.requests
    run(resumed.close())


def test_unknown_response_requires_explicit_retry(tmp_path):
    class Interrupted(FakeGateway):
        async def invoke(self, request):
            raise asyncio.CancelledError()

    runtime = make(tmp_path, model_gateway=Interrupted())
    with pytest.raises(asyncio.CancelledError):
        run(runtime.execute("task"))
    checkpoint = copy.deepcopy(runtime.state)
    run(runtime.close())
    gateway = FakeGateway()
    resumed = make(tmp_path, model_gateway=gateway, checkpoint=checkpoint)
    resumed.resume()
    with pytest.raises(HookRecoveryRequired):
        run(resumed.execute("task"))
    assert not gateway.requests
    resumed.resume(retry_unknown_models=True)
    assert run(resumed.execute("task")) == "done"
    assert len(gateway.requests) == 1
    run(resumed.close())


def test_budget_used_by_hook_repreparation_does_not_mark_unsent_action_unknown(tmp_path):
    from agentloom_runtime.module_contracts import ExecutionLimits, ModelRequest

    class Policy(NoCompaction):
        module_id = "counted-hook-compaction/v1"

        async def compact(self, context, model):
            if context.messages[-1]["content"] == "extra":
                reply = await model(ModelRequest([{"role": "user", "content": "summarize"}], []))
                return CompactionResult(
                    [*context.messages[:-1], {"role": "user", "content": reply["content"]}]
                )
            return None

    async def before(ctx, value):
        return PatchInput(
            {
                "messages": [
                    *[dict(item) for item in value["messages"]],
                    {"role": "user", "content": "extra"},
                ]
            }
        )

    gateway = FakeGateway([answer("summary"), answer("done")])
    runtime = make(
        tmp_path,
        model_gateway=gateway,
        compaction_policy=Policy(),
        hooks=manager(("model.chat.before", before)),
        limits=ExecutionLimits(1, 4),
    )
    with pytest.raises(RuntimeError, match="执行预算"):
        run(runtime.execute("task"))
    action = next(
        op for op in runtime.state["operations"].values() if op["scope"]["purpose"] == "action"
    )
    assert action["actual_status"] == "not_started"
    assert len(gateway.requests) == 1 and gateway.requests[0].purpose == "compaction"
    runtime.resume()
    assert run(runtime.execute("task")) == "done"
    assert len(gateway.requests) == 2
    run(runtime.close())


def test_async_budget_validator_is_awaited_after_hook_patch(tmp_path):
    class Policy(NoCompaction):
        module_id = "async-budget/v1"

        async def validate_request(self, request):
            if request.messages[-1].get("content") == "oversized":
                raise RuntimeError("async budget exceeded")

    async def before(ctx, value):
        return PatchInput(
            {
                "messages": [
                    *[dict(item) for item in value["messages"]],
                    {"role": "user", "content": "oversized"},
                ]
            }
        )

    gateway = FakeGateway()
    runtime = make(
        tmp_path,
        model_gateway=gateway,
        compaction_policy=Policy(),
        hooks=manager(("model.chat.before", before)),
    )
    with pytest.raises(RuntimeError, match="async budget exceeded"):
        run(runtime.execute("task"))
    assert not gateway.requests
    run(runtime.close())


def test_new_user_input_can_satisfy_rejected_model_before(tmp_path):
    from agentloom_runtime.hooks import HookRejected, Reject

    async def before(ctx, value):
        if not any("required detail" in (item.get("content") or "") for item in value["messages"]):
            return Reject("detail_needed", "provide detail")
        return Continue()

    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, hooks=manager(("model.chat.before", before)))
    with pytest.raises(HookRejected, match="detail_needed"):
        run(runtime.execute("task"))
    assert not gateway.requests
    runtime.resume("required detail")
    assert run(runtime.execute("task")) == "done"
    assert len(gateway.requests) == 1
    run(runtime.close())
