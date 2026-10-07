"""Exercise module replacement through public factory ports, without patching the Loop."""

import asyncio
import copy

import pytest
from agentloom_runtime.context import CharacterCompactionPolicy
from agentloom_runtime.execution_strategy import ReactStrategy
from agentloom_runtime.module_contracts import (
    CompactionResult,
    CompletionDecision,
    ExecutionLimits,
    ModelRequest,
)
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import ToolPersistenceError
from agentloom_runtime.tool_runtime import ToolRegistry
from test_harness import answer, call, snapshot


class FakeGateway:
    module_id = "test-model/v1"

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests = []
        self.closed = 0

    async def invoke(self, request):
        self.requests.append(copy.deepcopy(request))
        # Inputs are owned by the call; changing them cannot rewrite host history.
        request.messages[0]["content"] = "mutated by model adapter"
        return self.replies.pop(0) if self.replies else answer("done")

    async def aclose(self):
        self.closed += 1


class Accept:
    module_id = "test-completion/v1"

    async def review(self, context, model):
        context.messages.clear()
        context.plan.append({"id": "uncommitted"})
        return CompletionDecision("complete", "test evidence")


class NoCompaction:
    module_id = "test-compaction/v1"

    async def compact(self, context, model):
        context.messages.clear()
        context.plan.append({"id": "uncommitted"})
        return None


def make(tmp_path, **kwargs):
    defaults = dict(
        model_gateway=FakeGateway(),
        completion_policy=Accept(),
        compaction_policy=NoCompaction(),
    )
    defaults.update(kwargs)
    return create_engine(snapshot(), tmp_path, lambda *a: None, lambda s: s, None, **defaults)


def test_replaced_modules_execute_without_builtin_provider_or_shared_state(tmp_path):
    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway)

    async def run():
        assert await runtime.execute("question") == "done"
        await runtime.close()
        await runtime.close()

    asyncio.run(run())
    frame = runtime.state["frames"]["main"]
    assert frame["plan"] == []
    assert frame["messages"][0]["content"].startswith("完成用户任务")
    assert len(frame["messages"]) == 3
    assert len(gateway.requests) == 1
    assert gateway.closed == 1
    assert not hasattr(runtime, "decrypt") and not hasattr(runtime, "snapshot")


def test_complete_registry_replaces_builtins_and_freezes(tmp_path):
    registry = ToolRegistry()
    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, registry=registry)
    asyncio.run(runtime.execute("plain answer"))
    assert gateway.requests[0].tools == []
    assert registry.frozen
    asyncio.run(runtime.close())


def test_replacement_strategy_controls_tool_admission_and_candidate(tmp_path):
    class Strategy(ReactStrategy):
        module_id = "test-strategy/v1"

        def instructions(self, config, instance):
            return "CUSTOM_STRATEGY"

        def authorize_tool(self, definition, plan, instance):
            raise ValueError("custom strategy rejected action")

    gateway = FakeGateway(
        [call("workspace_write", {"path": "no.txt", "content": "no"}), answer("done")]
    )
    runtime = make(tmp_path, model_gateway=gateway, strategy=Strategy())
    asyncio.run(runtime.execute("task"))
    assert "CUSTOM_STRATEGY" in gateway.requests[0].messages[0]["content"]
    assert not (tmp_path / "no.txt").exists()
    assert (
        "custom strategy rejected action"
        in runtime.state["frames"]["main"]["messages"][3]["content"]
    )
    asyncio.run(runtime.close())


@pytest.mark.parametrize(
    "replacement",
    [
        [{"role": "system", "content": "replaced"}],
        "orphan-tool",
    ],
)
def test_invalid_compaction_cannot_commit_a_broken_context(tmp_path, replacement):
    class Broken:
        module_id = "broken/v1"

        async def compact(self, context, model):
            messages = replacement
            if messages == "orphan-tool":
                messages = [
                    context.messages[0],
                    {"role": "tool", "tool_call_id": "missing", "content": "x"},
                ]
            return CompactionResult(messages)

    runtime = make(tmp_path, compaction_policy=Broken())
    with pytest.raises(ValueError, match="压缩结果"):
        asyncio.run(runtime.execute("keep task"))
    assert runtime.state["frames"]["main"]["messages"][-1]["content"] == "keep task"
    assert runtime.modules.model.requests == []
    asyncio.run(runtime.close())


def test_policy_calls_keep_host_purpose_instance_and_budget(tmp_path):
    class Reviewer:
        module_id = "reviewer/v1"

        async def review(self, context, model):
            await model(
                ModelRequest([{"role": "user", "content": "review"}], [], "action", "other")
            )
            return CompletionDecision("complete", "ok")

    gateway = FakeGateway()
    runtime = make(
        tmp_path, model_gateway=gateway, completion_policy=Reviewer(), limits=ExecutionLimits(2, 2)
    )
    asyncio.run(runtime.execute("task"))
    assert [(r.purpose, r.instance) for r in gateway.requests] == [
        ("action", "main"),
        ("verification", "main"),
    ]
    assert runtime.state["calls"] == 2
    asyncio.run(runtime.close())


def test_policy_cannot_send_action_tools(tmp_path):
    class Reviewer:
        module_id = "reviewer/v1"

        async def review(self, context, model):
            await model(ModelRequest([], [{"type": "function"}]))

    runtime = make(tmp_path, completion_policy=Reviewer())
    with pytest.raises(ValueError, match="不能调用行动工具"):
        asyncio.run(runtime.execute("task"))
    assert len(runtime.modules.model.requests) == 1
    asyncio.run(runtime.close())


def test_checkpoint_module_bindings_match_before_resuming(tmp_path):
    saved = []
    runtime = make(tmp_path, save=lambda value: saved.append(copy.deepcopy(value)))
    asyncio.run(runtime.execute("task"))
    checkpoint = saved[-1]
    asyncio.run(runtime.close())
    resumed = make(tmp_path, checkpoint=checkpoint)
    resumed.resume("continue")
    assert asyncio.run(resumed.execute("task")) == "done"
    asyncio.run(resumed.close())
    with pytest.raises(ValueError, match="模块版本或配置不兼容"):
        make(tmp_path, checkpoint=checkpoint, limits=ExecutionLimits(1, 1))
    with pytest.raises(ValueError, match="模块版本或配置不兼容"):
        make(
            tmp_path, checkpoint=checkpoint, compaction_policy=CharacterCompactionPolicy(2000, 500)
        )
    checkpoint.pop("modules")
    with pytest.raises(ValueError, match="旧检查点"):
        make(tmp_path, checkpoint=checkpoint)


def test_legacy_checkpoint_restores_with_default_modules(tmp_path):
    state = {
        "format": 1,
        "calls": 0,
        "total_calls": 0,
        "delegations": 0,
        "citations": {},
        "frames": {},
    }
    runtime = create_engine(
        snapshot(), tmp_path, lambda *a: None, lambda s: s, None, checkpoint=state
    )
    assert runtime.state["modules"]["model"]["implementation"] == "chat-completions/v1"
    asyncio.run(runtime.close())


def test_checkpoint_save_failure_is_not_a_tool_result(tmp_path):
    def fail(state):
        raise OSError("storage unavailable")

    runtime = make(tmp_path, save=fail)
    with pytest.raises(ToolPersistenceError):
        asyncio.run(runtime.execute("task"))
    assert runtime.modules.model.requests == []
    asyncio.run(runtime.close())


def test_all_owned_modules_close_when_one_cleanup_fails(tmp_path):
    class BrokenCleanup(Accept):
        async def aclose(self):
            raise RuntimeError("cleanup failed")

    gateway = FakeGateway()
    runtime = make(tmp_path, model_gateway=gateway, completion_policy=BrokenCleanup())
    with pytest.raises(ExceptionGroup):
        asyncio.run(runtime.close())
    assert gateway.closed == 1


def test_close_cancels_and_waits_for_model_before_releasing_gateway(tmp_path):
    async def run():
        entered, cleaned = asyncio.Event(), asyncio.Event()

        class Slow(FakeGateway):
            async def invoke(self, request):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaned.set()

            async def aclose(self):
                assert cleaned.is_set()
                await super().aclose()

        gateway = Slow()
        runtime = make(tmp_path, model_gateway=gateway)
        task = asyncio.create_task(runtime.execute("task"))
        await entered.wait()
        with pytest.raises(RuntimeError, match="并发"):
            await runtime.execute("second")
        await asyncio.gather(runtime.close(), runtime.close())
        assert task.cancelled() and gateway.closed == 1
        with pytest.raises(RuntimeError, match="已关闭"):
            await runtime.execute("later")

    asyncio.run(run())


def test_invalid_gateway_response_is_rejected_before_pending_or_side_effects(tmp_path):
    invalid = call("workspace_write", {"path": "bad.txt", "content": "bad"})
    invalid["tool_calls"] *= 2
    runtime = make(tmp_path, model_gateway=FakeGateway([invalid]))
    with pytest.raises(ValueError, match="协议不合法"):
        asyncio.run(runtime.execute("task"))
    assert runtime.state["frames"]["main"]["pending"] is None
    assert not (tmp_path / "bad.txt").exists()
    asyncio.run(runtime.close())


def test_provider_binding_ignores_secret_rotation_but_rejects_model_change(tmp_path):
    snap = snapshot()
    snap["model_obj"]["base_url"] = "https://example.test/v1"

    def build(value, checkpoint=None):
        return create_engine(
            value, tmp_path, lambda *a: None, lambda s: s, None, checkpoint=checkpoint
        )

    original = build(snap)
    checkpoint = copy.deepcopy(original.state)
    asyncio.run(original.close())
    snap["model_obj"]["secret"] = "rotated"
    rotated = build(snap, checkpoint)
    assert "rotated" not in str(rotated.state["modules"])
    asyncio.run(rotated.close())
    snap["model_obj"]["model_id"] = "different-model"
    with pytest.raises(ValueError, match="不兼容"):
        build(snap, checkpoint)


def test_legacy_checkpoint_rejects_changed_default_policy_configuration(tmp_path):
    state = {
        "format": 1,
        "calls": 0,
        "total_calls": 0,
        "delegations": 0,
        "citations": {},
        "frames": {},
    }
    with pytest.raises(ValueError, match="显式迁移"):
        create_engine(
            snapshot(),
            tmp_path,
            lambda *a: None,
            lambda s: s,
            None,
            checkpoint=state,
            compaction_policy=CharacterCompactionPolicy(2000, 500),
        )


def test_checkpoint_rejects_unversioned_or_changed_tool_implementation(tmp_path):
    from agentloom_runtime.tool_contracts import ToolDefinition, parameters

    async def handler(context, args):
        return "ok"

    def registry(version):
        result = ToolRegistry()
        result.register(
            ToolDefinition(
                "custom",
                "custom tool",
                parameters({}),
                handler,
                implementation_id=version,
            )
        )
        return result

    original = make(tmp_path, registry=registry("custom/v1"))
    checkpoint = copy.deepcopy(original.state)
    asyncio.run(original.close())
    import json

    # Actual persistence uses JSON; the round trip must preserve compatibility.
    resumed = make(
        tmp_path, registry=registry("custom/v1"), checkpoint=json.loads(json.dumps(checkpoint))
    )
    asyncio.run(resumed.close())
    with pytest.raises(ValueError, match="不兼容"):
        make(tmp_path, registry=registry("custom/v2"), checkpoint=checkpoint)
    with pytest.raises(ValueError, match="implementation_id"):
        make(tmp_path, registry=registry(None), checkpoint=checkpoint)


def test_legacy_checkpoint_cannot_silently_drop_builtin_tool_catalog(tmp_path):
    state = {
        "format": 1,
        "calls": 0,
        "total_calls": 0,
        "delegations": 0,
        "citations": {},
        "frames": {},
    }
    with pytest.raises(ValueError, match="工具集合需要显式迁移"):
        create_engine(
            snapshot(),
            tmp_path,
            lambda *a: None,
            lambda s: s,
            None,
            checkpoint=state,
            registry=ToolRegistry(),
        )
