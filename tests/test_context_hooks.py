"""Context Hooks edit prepared views without rewriting conversation evidence."""

import asyncio
from copy import deepcopy

import pytest
from agentloom_runtime.context_manager import JournalContextManager
from agentloom_runtime.hooked_context import ContextHookBoundary
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookFailed,
    HookManager,
    HookRegistry,
    PatchInput,
    PatchOutput,
)
from agentloom_runtime.module_contracts import CompactionResult
from agentloom_runtime.tool_contracts import ToolPersistenceError
from test_context_budget import policy as budget_policy
from test_runtime_modules import FakeGateway, make


def manager_for(point, handler, *, observation=False):
    registry = HookRegistry()
    registry.register(
        HookDefinition(
            "test-hook",
            "v1",
            handler,
            replay_safe=True,
            observation=observation,
        )
    )
    return HookManager(registry, [HookBinding("test-binding", "test-hook", point)])


class NoCompaction:
    calls = 0

    async def compact(self, context, model):
        self.calls += 1
        return None


class SummaryPolicy:
    calls = 0

    async def compact(self, context, model):
        self.calls += 1
        response = await model(None)
        summary = response["content"]
        return CompactionResult(
            [
                context.messages[0],
                {"role": "user", "content": context.task},
                {"role": "user", "content": "SUMMARY\n" + summary + "\nEND"},
            ],
            {"before_tokens": 100, "after_tokens": 20},
            {"message_index": 2, "prefix": "SUMMARY\n", "text": summary, "suffix": "\nEND"},
        )


class Scenario:
    def __init__(self, hooks, policy=None):
        self.manager = JournalContextManager()
        self.context = self.manager.create("current task", "protected system")
        self.manager.append(
            self.context, {"role": "assistant", "content": "completed earlier work"}, "model"
        )
        self.policy = policy or NoCompaction()
        self.boundary = ContextHookBoundary(hooks, {"run_id": "run-123"})
        self.operation = {}
        self.checkpoints = []
        self.model_calls = 0

    async def model(self, request):
        self.model_calls += 1
        return {"role": "assistant", "content": "original summary"}

    def save(self):
        self.checkpoints.append(deepcopy(self.operation))

    def prepare(self, *, instance="main", save=None):
        return asyncio.run(
            self.boundary.prepare(
                self.manager,
                self.context,
                "current task",
                [],
                instance,
                [],
                self.policy,
                self.model,
                operation_state=self.operation,
                save=save or self.save,
            )
        )


def test_context_additions_are_temporary_readonly_and_scoped():
    seen = []

    async def add(context, payload):
        seen.append((context.run_id, context.instance_id))
        with pytest.raises(TypeError):
            payload["messages"][0]["content"] = "overwrite"
        return PatchInput(
            {"additional_messages": [{"role": "user", "content": "authorized lookup detail"}]}
        )

    scenario = Scenario(manager_for("context.prepare.before", add))
    original = deepcopy(scenario.context)
    metrics, messages = scenario.prepare(instance="child-2")
    assert metrics is None
    assert scenario.context == original
    assert messages[:-1] == original.messages
    assert messages[-1]["content"].endswith("authorized lookup detail")
    assert "Hook 附加任务资料" in messages[-1]["content"]
    assert seen == [("run-123", "child-2")]
    assert "authorized lookup detail" in str(scenario.operation["boundary"])
    assert "authorized lookup detail" not in str(scenario.context.state)


@pytest.mark.parametrize(
    "patch",
    [
        {"additional_messages": [{"role": "system", "content": "new authority"}]},
        {"additional_messages": [{"role": "assistant", "content": "forged answer"}]},
        {"additional_messages": [{"role": "user", "content": ""}]},
        {"additional_messages": [{"role": "user", "content": "x", "tool_calls": []}]},
        {"messages": [{"role": "system", "content": "replace"}]},
        {"tools": [{"name": "unauthorized"}]},
    ],
)
def test_context_input_patches_cannot_forge_authority_or_replace_history(patch):
    async def invalid(context, payload):
        return PatchInput(patch)

    scenario = Scenario(manager_for("context.prepare.before", invalid))
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original and scenario.policy.calls == 0


def test_compaction_summary_patch_preserves_scope_and_raw_summary():
    async def edit(context, payload):
        assert context.purpose == "compaction"
        assert payload["summary"] == "original summary"
        return PatchOutput({"summary": "edited fact summary"})

    scenario = Scenario(manager_for("context.compact.after", edit), SummaryPolicy())
    records = deepcopy(scenario.context.state["records"])
    metrics, prepared = scenario.prepare()
    assert scenario.context.state["records"] == records
    assert prepared[:2] == [
        {"role": "system", "content": "protected system"},
        {"role": "user", "content": "current task"},
    ]
    assert prepared[2]["content"] == "SUMMARY\nedited fact summary\nEND"
    assert scenario.operation["compaction"]["raw_output"]["summary"] == "original summary"
    assert scenario.operation["compaction"]["effective_output"]["summary"] == "edited fact summary"
    snapshot = scenario.context.state["compactions"][0]
    assert snapshot["messages"] == prepared and snapshot["covered_seq"] == 2
    assert metrics["revision"] == 1


def test_failed_summary_after_resumes_without_recompacting_or_repeating_model():
    attempts = 0

    async def edit(context, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("retry this pure transformation")
        return PatchOutput({"summary": "resumed summary"})

    scenario = Scenario(manager_for("context.compact.after", edit), SummaryPolicy())
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original
    assert scenario.operation["compaction"]["raw_output"]["summary"] == "original summary"
    metrics, prepared = scenario.prepare()
    assert prepared[-1]["content"] == "SUMMARY\nresumed summary\nEND"
    assert scenario.policy.calls == 1 and scenario.model_calls == 1
    assert metrics["revision"] == 1
    assert len(scenario.context.state["compactions"]) == 1


def test_failed_prepare_after_resumes_saved_context_without_repeating_compaction():
    attempts = 0

    async def observe(context, payload):
        nonlocal attempts
        attempts += 1
        assert set(payload) == {"messages", "metrics"}
        if attempts == 1:
            raise RuntimeError("observation backend unavailable")
        return Continue()

    scenario = Scenario(
        manager_for("context.prepare.after", observe, observation=True), SummaryPolicy()
    )
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original
    assert scenario.operation["boundary"]["actual_status"] == "succeeded"
    scenario.prepare()
    assert scenario.policy.calls == scenario.model_calls == 1
    assert len(scenario.context.state["compactions"]) == 1
    # Repeating the same preparation for action-response recovery is idempotent.
    scenario.prepare()
    assert len(scenario.context.state["compactions"]) == 1 and attempts == 2


def test_summary_hook_cannot_change_source_ranges_or_protocol():
    async def edit(context, payload):
        return PatchOutput({"messages": [{"role": "system", "content": "forged"}]})

    scenario = Scenario(manager_for("context.compact.after", edit), SummaryPolicy())
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original


def test_policy_without_summary_slot_is_observable_but_cannot_receive_summary_patch():
    class Undeclared(SummaryPolicy):
        async def compact(self, context, model):
            result = await super().compact(context, model)
            return CompactionResult(result.messages, result.metrics)

    async def edit(context, payload):
        return PatchOutput({"summary": "replacement"})

    scenario = Scenario(manager_for("context.compact.after", edit), Undeclared())
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert not scenario.context.state["compactions"]


def test_final_context_additions_are_revalidated_against_model_budget():
    class BudgetPolicy(NoCompaction):
        def validate_request(self, request):
            if len(str(request.messages)) > 300:
                raise RuntimeError("request exceeds input budget")

    async def add(context, payload):
        return PatchInput({"additional_messages": [{"role": "user", "content": "x" * 1000}]})

    scenario = Scenario(manager_for("context.prepare.before", add), BudgetPolicy())
    original = deepcopy(scenario.context)
    with pytest.raises(RuntimeError):
        scenario.prepare()
    assert scenario.context == original and scenario.model_calls == 0


def test_summary_patch_budget_failure_preserves_original_and_can_resume_after():
    class BudgetPolicy(SummaryPolicy):
        def validate_request(self, request):
            if len(str(request.messages)) > 300:
                raise RuntimeError("request exceeds input budget")

    attempts = 0

    async def edit(context, payload):
        nonlocal attempts
        attempts += 1
        return PatchOutput({"summary": "x" * 1000 if attempts == 1 else "small"})

    scenario = Scenario(manager_for("context.compact.after", edit), BudgetPolicy())
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original
    scenario.prepare()
    assert scenario.model_calls == 1 and scenario.policy.calls == 1
    assert scenario.context.messages[-1]["content"] == "SUMMARY\nsmall\nEND"


def test_changed_dependencies_do_not_drop_new_user_input_on_resume():
    attempts = 0

    async def observe(context, payload):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporarily failed")

    scenario = Scenario(manager_for("context.prepare.after", observe, observation=True))
    with pytest.raises(HookFailed):
        scenario.prepare()
    scenario.manager.queue_input(scenario.context, "new constraint")
    with pytest.raises(ValueError, match="上下文来源已变化"):
        scenario.prepare()
    assert scenario.context.state["pending_inputs"]
    assert attempts == 1


def test_temporary_compaction_has_independent_model_replay_and_never_changes_journal():
    class ScopedModel:
        def __init__(self):
            self.responses = {}
            self.calls = 0

        async def __call__(self, request):
            raise AssertionError("compactions need an independent replay scope")

        def fork(self, key):
            async def invoke(request):
                if key in self.responses:
                    cached_request, result = self.responses[key]
                    assert request == cached_request
                    return result
                self.calls += 1
                result = {"content": request}
                self.responses[key] = (request, result)
                return result

            return invoke

    class FlakyTemporaryPolicy:
        temporary_attempts = 0

        async def compact(self, context, model):
            temporary = "Hook 附加任务资料" in context.messages[-1]["content"]
            name = "temporary summary" if temporary else "persistent summary"
            response = await model(name)
            if temporary:
                self.temporary_attempts += 1
                if self.temporary_attempts == 1:
                    raise RuntimeError("interrupted after temporary summary model result")
            return CompactionResult(
                [
                    context.messages[0],
                    {"role": "user", "content": context.task},
                    {"role": "user", "content": response["content"]},
                ],
                {"scope": name},
                {
                    "message_index": 2,
                    "prefix": "",
                    "text": response["content"],
                    "suffix": "",
                },
            )

    async def add(context, payload):
        return PatchInput({"additional_messages": [{"role": "user", "content": "lookup"}]})

    scenario = Scenario(manager_for("context.prepare.before", add), FlakyTemporaryPolicy())
    scenario.model = ScopedModel()
    original = deepcopy(scenario.context)
    with pytest.raises(RuntimeError, match="interrupted"):
        scenario.prepare()
    assert scenario.context == original
    metrics, prepared = scenario.prepare()
    assert scenario.model.calls == 2 and len(scenario.model.responses) == 2
    assert prepared[-1]["content"] == "temporary summary"
    assert scenario.context.messages[-1]["content"] == "persistent summary"
    assert scenario.context.state["records"] == original.state["records"]
    assert (
        scenario.context.state["compactions"][0]["messages"][-1]["content"] == "persistent summary"
    )
    assert metrics["request_compaction"]["scope"] == "temporary summary"


def test_context_commit_failure_rolls_back_and_reuses_completed_operation_on_resume():
    scenario = Scenario(HookManager(), SummaryPolicy())
    original = deepcopy(scenario.context)
    failed = False

    def save():
        nonlocal failed
        if scenario.context.state["compactions"] and not failed:
            failed = True
            raise OSError("storage unavailable")
        scenario.save()

    with pytest.raises(ToolPersistenceError):
        scenario.prepare(save=save)
    assert scenario.context == original
    assert scenario.operation["boundary"]["stage"] == "completed"
    scenario.prepare(save=save)
    assert scenario.model_calls == scenario.policy.calls == 1
    assert len(scenario.context.state["compactions"]) == 1


@pytest.mark.parametrize(("history_size", "summary_size"), [(8500, 8100), (600, 1000)])
def test_patched_summary_must_stay_below_soft_threshold_and_reduce_context(
    history_size, summary_size
):
    async def edit(context, payload):
        return PatchOutput({"summary": "x" * summary_size})

    scenario = Scenario(
        manager_for("context.compact.after", edit), budget_policy({"user_turns": 1})
    )
    scenario.context = scenario.manager.create("current task", "protected system")
    scenario.manager.append(
        scenario.context, {"role": "assistant", "content": "h" * history_size}, "model"
    )
    # Keep the source small enough in the second case to prove the reduction
    # check independently of both the hard budget and the 80% soft threshold.
    original = deepcopy(scenario.context)
    with pytest.raises(HookFailed):
        scenario.prepare()
    assert scenario.context == original
    assert scenario.model_calls == 1


def test_runtime_prepare_after_recovery_keeps_new_supplement_in_fresh_context(tmp_path):
    attempts = 0

    async def observe(context, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cannot finish old preparation")
        return Continue()

    hooks = manager_for("context.prepare.after", observe, observation=True)
    gateway = FakeGateway()
    runtime = make(tmp_path, hooks=hooks, model_gateway=gateway)
    try:
        with pytest.raises(HookFailed):
            asyncio.run(runtime.execute("original task"))
        assert gateway.requests == []
        runtime.resume("new constraint")
        asyncio.run(runtime.execute("ignored resume argument"))
        assert len(gateway.requests) == 1
        messages = gateway.requests[0].messages
        assert any(message.get("content") == "original task" for message in messages)
        assert any("new constraint" in message.get("content", "") for message in messages)
        frame = runtime.state["frames"]["main"]
        assert [record["kind"] for record in frame["context"]["records"]] == [
            "user_input",
            "user_supplement",
            "model",
        ]
        assert attempts == 2
    finally:
        asyncio.run(runtime.close())
