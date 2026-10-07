"""Source history, prepared context and recovery remain independent."""

import asyncio
from copy import deepcopy

import pytest
from agentloom_runtime.context_manager import JournalContextManager, queue_instruction
from agentloom_runtime.module_contracts import (
    CompactionResult,
    CompletionDecision,
    ExecutionLimits,
    ModelRequest,
)
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import ToolDefinition
from test_harness import answer, call, snapshot
from test_runtime_modules import FakeGateway, NoCompaction, make


async def unused_model(request):
    raise AssertionError("unexpected model call")


class SnapshotPolicy:
    module_id = "snapshot-test/v1"

    def __init__(self):
        self.inputs = []

    async def compact(self, context, model):
        self.inputs.append(deepcopy(context))
        return CompactionResult(
            [context.messages[0], {"role": "user", "content": "summary of earlier work"}],
            {"reason": "test"},
        )


def test_raw_records_exclude_inherited_history_and_survive_compaction():
    manager = JournalContextManager()
    context = manager.create(
        "new task",
        "system prompt",
        [{"role": "user", "content": "old task"}, answer("old result")],
        {"watermark": 40, "user_turns": 4, "model_steps": 12, "baseline_model_steps": 8},
    )
    manager.append(context, answer("new result"), "model", model_step=True)
    before = deepcopy(context.state["records"])
    policy = SnapshotPolicy()
    result = asyncio.run(
        manager.prepare(
            context, "new task", [], "main", [{"type": "function"}], policy, unused_model
        )
    )
    assert context.state["records"] == before
    assert [record["seq"] for record in before] == [1, 2]
    assert [record["message"]["content"] for record in before] == ["new task", "new result"]
    assert context.state["user_turns"] == 5
    assert context.state["model_steps"] == 13
    assert context.state["baseline_user_turns"] == 5
    assert context.state["baseline_model_steps"] == 13
    snapshot = context.state["compactions"][0]
    assert snapshot["covered_seq"] == 2 and snapshot["history_watermark"] == 40
    assert snapshot["messages"] == context.messages
    assert policy.inputs[0].tools == [{"type": "function"}]
    assert policy.inputs[0].counters["baseline_model_steps"] == 8
    assert result["revision"] == 1
    context.messages[-1]["content"] = "future view mutation"
    assert snapshot["messages"][-1]["content"] == "summary of earlier work"


def test_failed_compaction_does_not_rewrite_raw_view_or_baseline():
    class Invalid(SnapshotPolicy):
        async def compact(self, context, model):
            return CompactionResult([{"role": "system", "content": "replaced system"}])

    manager = JournalContextManager()
    context = manager.create("task", "system")
    before = deepcopy(context)
    with pytest.raises(ValueError, match="压缩结果"):
        asyncio.run(manager.prepare(context, "task", [], "main", [], Invalid(), unused_model))
    assert context == before


def test_compaction_baselines_and_local_sequence_survive_restore():
    manager = JournalContextManager()
    context = manager.create(
        "task", "system", history_metadata={"user_turns": 9, "model_steps": 22}
    )
    manager.append(context, answer("done"), "model", model_step=True)
    asyncio.run(manager.prepare(context, "task", [], "main", [], SnapshotPolicy(), unused_model))
    restored = manager.restore(deepcopy(context.messages), deepcopy(context.state), "task")
    manager.queue_input(restored, "continue with one more check")
    manager.drain_inputs(restored)
    assert restored.state["user_turns"] == restored.state["baseline_user_turns"] == 10
    assert restored.state["model_steps"] == restored.state["baseline_model_steps"] == 23
    manager.append(restored, answer("checked"), "model", model_step=True)
    policy = SnapshotPolicy()
    asyncio.run(manager.prepare(restored, "task", [], "main", [], policy, unused_model))
    assert (
        policy.inputs[0].counters["model_steps"] - policy.inputs[0].counters["baseline_model_steps"]
        == 1
    )
    assert restored.state["compactions"][-1]["revision"] == 2
    assert restored.state["compactions"][-1]["covered_seq"] == 4


def test_auxiliary_request_budget_guard_runs_before_provider_or_budget_consumption(tmp_path):
    class Guard(NoCompaction):
        def validate_request(self, request):
            request.messages[0]["content"] = "adapter-owned copy"
            if request.purpose == "verification":
                raise RuntimeError("request does not fit")

    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, compaction_policy=Guard())
    messages = [{"role": "user", "content": "original"}]
    with pytest.raises(RuntimeError, match="does not fit"):
        asyncio.run(runtime.model(ModelRequest(messages, [], "verification")))
    assert messages[0]["content"] == "original"
    assert gateway.requests == [] and runtime.state["calls"] == 0
    asyncio.run(runtime.close())


def test_full_tool_output_is_retained_even_when_model_view_is_shortened(tmp_path):
    async def large_result(arguments, context):
        return {"text": "x" * 30000}

    def tools(registry):
        registry.register(
            ToolDefinition(
                "large_result",
                "read large result",
                {"type": "object", "properties": {}},
                large_result,
                implementation_id="test-large/v1",
            )
        )

    gateway = FakeGateway([call("large_result", {}), answer("done")])
    runtime = make(tmp_path, model_gateway=gateway, configure_tools=tools)
    asyncio.run(runtime.execute("read result"))
    frame = runtime.state["frames"]["main"]
    raw = next(
        record["message"] for record in frame["context"]["records"] if record["kind"] == "tool"
    )
    visible = next(message for message in frame["messages"] if message["role"] == "tool")
    assert len(raw["content"]) > 30000
    assert len(visible["content"]) < 25000 and "仅展示前段" in visible["content"]
    assert frame["context"]["model_steps"] == 2
    asyncio.run(runtime.close())


def test_resume_records_supplement_before_pending_tools_but_defers_model_message(tmp_path):
    runtime = make(
        tmp_path,
        model_gateway=FakeGateway([call("workspace_list", {})]),
        limits=ExecutionLimits(10, 1),
    )
    with pytest.raises(RuntimeError, match="轮次限制"):
        asyncio.run(runtime.execute("inspect workspace"))
    checkpoint = deepcopy(runtime.state)
    asyncio.run(runtime.close())
    queue_instruction(checkpoint, "also report failures")
    frame = checkpoint["frames"]["main"]
    assert frame["context"]["records"][-1]["kind"] == "user_supplement"
    assert frame["messages"][-1]["role"] == "assistant"
    assert frame["context"]["user_turns"] == 1

    gateway = FakeGateway([answer("done")])
    resumed = make(
        tmp_path, model_gateway=gateway, checkpoint=checkpoint, limits=ExecutionLimits(10, 1)
    )
    resumed.resume()
    with pytest.raises(RuntimeError, match="轮次限制"):
        asyncio.run(resumed.execute("ignored", history=[answer("unrelated later task")]))
    frame = resumed.state["frames"]["main"]
    assert [message["role"] for message in gateway.requests[0].messages[-2:]] == ["tool", "user"]
    records = frame["context"]["records"]
    assert [record["kind"] for record in records] == [
        "user_input",
        "model",
        "user_supplement",
        "tool",
        "model",
    ]
    assert frame["context"]["model_steps"] == 2
    assert not frame["context"]["pending_inputs"]
    assert "unrelated later task" not in str(frame)
    asyncio.run(resumed.close())


def test_input_accepted_before_runtime_initialization_is_applied_once(tmp_path):
    runtime = make(tmp_path)
    queue_instruction(runtime.state, "extra constraint")
    runtime.resume()
    asyncio.run(runtime.execute("task"))
    context = runtime.state["frames"]["main"]["context"]
    assert [record["kind"] for record in context["records"]] == [
        "user_input",
        "user_supplement",
        "model",
    ]
    assert context["user_turns"] == 1 and not context["pending_inputs"]
    assert "pending_inputs" not in runtime.state
    asyncio.run(runtime.close())


def test_context_manager_is_injected_bound_and_used_for_each_message(tmp_path):
    class Tracking(JournalContextManager):
        module_id = "tracking-context/v1"

        def __init__(self):
            self.kinds = []

        def append(self, context, message, kind, **kwargs):
            self.kinds.append(kind)
            return super().append(context, message, kind, **kwargs)

    context_manager = Tracking()
    runtime = make(tmp_path, context_manager=context_manager)
    asyncio.run(runtime.execute("task", history_metadata={"user_turns": 5, "model_steps": 9}))
    assert context_manager.kinds == ["user_input", "model"]
    checkpoint = deepcopy(runtime.state)
    assert checkpoint["modules"]["context"]["implementation"] == "tracking-context/v1"
    asyncio.run(runtime.close())
    with pytest.raises(ValueError, match="模块版本或配置不兼容"):
        make(tmp_path, checkpoint=checkpoint)
    restored = make(tmp_path, checkpoint=checkpoint, context_manager=Tracking())
    restored.resume("continue")
    assert restored.state["frames"]["main"]["context"]["user_turns"] == 6
    assert restored.state["frames"]["main"]["context"]["model_steps"] == 10
    asyncio.run(restored.close())


def test_pre_context_checkpoints_use_default_manager_with_partial_source_marker(tmp_path):
    runtime = make(tmp_path)
    asyncio.run(runtime.execute("task"))
    checkpoint = deepcopy(runtime.state)
    del checkpoint["modules"]["context"]
    del checkpoint["frames"]["main"]["context"]
    asyncio.run(runtime.close())
    restored = make(tmp_path, checkpoint=checkpoint)
    asyncio.run(restored.execute("task"))
    context = restored.state["frames"]["main"]["context"]
    assert context["raw_complete"] is False
    assert context["records"][0] == {
        "seq": 1,
        "kind": "user_input",
        "message": {"role": "user", "content": "task"},
    }
    asyncio.run(restored.close())

    class Replacement(JournalContextManager):
        module_id = "replacement/v1"

    with pytest.raises(ValueError, match="上下文模块需要显式迁移"):
        make(tmp_path, checkpoint=checkpoint, context_manager=Replacement())


def test_legacy_compressed_context_recovers_known_original_input_without_invented_history():
    manager = JournalContextManager()
    context = manager.restore(
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "summary of lost history"},
        ],
        None,
        "original task\n用户补充：additional requirement",
    )
    assert context.state["raw_complete"] is False
    assert context.state["records"] == [
        {"seq": 1, "kind": "user_input", "message": {"role": "user", "content": "original task"}}
    ]
    assert context.messages[-1]["content"] == "summary of lost history"


def test_child_action_counts_stay_local_and_survive_interrupted_resume(tmp_path):
    child = {
        "id": "worker",
        "name": "child",
        "description": "执行子任务",
        "prompt": "perform child work",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    snap = snapshot(subs=[child])

    class InterruptedGateway:
        module_id = "child-counting-model/v1"

        def __init__(self, resume=False):
            self.resume, self.child_calls = resume, 0
            self.requests = []

        async def invoke(self, request):
            self.requests.append((request.instance, request.purpose))
            if request.purpose == "verification":
                return answer("verified")
            if self.resume:
                return answer("child done" if request.instance != "main" else "main done")
            if request.instance == "main":
                return call("delegate_task", {"subagent_id": "worker", "task": "write once"})
            self.child_calls += 1
            if self.child_calls == 1:
                return call("workspace_write", {"path": "child.txt", "content": "written"})
            raise asyncio.CancelledError()

    class Reviewing:
        module_id = "counting-review/v1"

        async def review(self, context, model):
            await model(ModelRequest([{"role": "user", "content": "check"}], []))
            return CompletionDecision("complete", "verified")

    def runtime(gateway, checkpoint=None):
        return create_engine(
            snap,
            tmp_path,
            lambda *args: None,
            lambda secret: secret,
            None,
            checkpoint=checkpoint,
            model_gateway=gateway,
            compaction_policy=NoCompaction(),
            completion_policy=Reviewing(),
        )

    initial = runtime(InterruptedGateway())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            initial.execute(
                "main task",
                history_metadata={"user_turns": 3, "model_steps": 9, "baseline_model_steps": 8},
            )
        )
    checkpoint = deepcopy(initial.state)
    asyncio.run(initial.close())
    assert checkpoint["frames"]["main"]["context"]["model_steps"] == 10
    assert checkpoint["frames"]["sub-1"]["context"]["model_steps"] == 1

    gateway = InterruptedGateway(resume=True)
    resumed = runtime(gateway, checkpoint)
    resumed.resume()
    assert resumed.state["frames"]["main"]["context"]["model_steps"] == 10
    assert resumed.state["frames"]["sub-1"]["context"]["model_steps"] == 1
    assert asyncio.run(resumed.execute("main task")) == "main done"
    contexts = {key: frame["context"] for key, frame in resumed.state["frames"].items()}
    assert contexts["main"]["model_steps"] == 11
    assert contexts["sub-1"]["model_steps"] == 2
    assert contexts["main"]["user_turns"] == 4 and contexts["sub-1"]["user_turns"] == 1
    assert contexts["main"]["baseline_model_steps"] == 8
    assert contexts["sub-1"]["baseline_model_steps"] == 0
    assert gateway.requests == [
        ("sub-1", "action"),
        ("sub-1", "verification"),
        ("main", "action"),
        ("main", "verification"),
    ]
    assert len([r for r in contexts["sub-1"]["records"] if r["kind"] == "tool"]) == 1
    asyncio.run(resumed.close())
