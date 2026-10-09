import asyncio
import copy

import pytest
from agentloom_runtime.embedding_hooks import EmbeddingHookBoundary
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookFailed,
    HookManager,
    HookRecoveryRequired,
    HookRegistry,
    HookRejected,
    PatchInput,
    PatchOutput,
    Reject,
)


def manager_for(handler, point="before", *, observation=False, **binding):
    registry = HookRegistry()
    registry.register(
        HookDefinition("test", "v1", handler, replay_safe=True, observation=observation)
    )
    return HookManager(
        registry,
        [HookBinding("test", "test", "model.embedding." + point, **binding)],
    )


def boundary(manager=None, **kwargs):
    return EmbeddingHookBoundary(
        manager or HookManager(), model_id="embed-1", index_signature="signature-1", **kwargs
    )


async def vectors(texts):
    return {
        "vectors": [[float(index), 1.0] for index in range(len(texts))],
        "indices": list(range(len(texts))),
    }


def call(owner, texts=None, invoke=vectors, state=None, save=lambda: None, **kwargs):
    return asyncio.run(
        owner.invoke(
            ["first", "second"] if texts is None else texts,
            invoke,
            state={} if state is None else state,
            save=save,
            **kwargs,
        )
    )


def test_embedding_hook_preserves_indexed_order_and_hides_credentials():
    seen = []

    async def clean(ctx, payload):
        assert ctx.space_id == "space-1"
        assert ctx.kb_id == "wiki-1" and ctx.index_revision == 3
        assert ctx.index_signature == "signature-1" and ctx.model_id == "embed-1"
        assert ctx.run_id is None and ctx.purpose == "document"
        assert "secret" not in payload and "base_url" not in payload
        with pytest.raises(TypeError):
            payload["texts"][0]["text"] = "changed"
        with pytest.raises(TypeError):
            ctx.scope["kb_id"] = "other"
        return PatchInput(
            {
                "texts": [
                    {"index": item["index"], "text": item["text"].strip()}
                    for item in payload["texts"]
                ]
            }
        )

    async def invoke(texts):
        seen.append(texts)
        return {
            "vectors": [[2, 3], [0, 1]],
            "indices": [1, 0],
            "usage": {"total_tokens": 4},
            "provider_request_id": "req-1",
            "secret": "hidden",
        }

    state = {}
    owner = boundary(
        manager_for(clean), scope={"space_id": "space-1", "kb_id": "wiki-1", "index_revision": 3}
    )
    result = call(owner, [" first ", " second "], invoke=invoke, state=state)
    assert seen == [["first", "second"]]
    assert result == {
        "vectors": [[0, 1], [2, 3]],
        "indices": [0, 1],
        "dimension": 2,
        "usage": {"total_tokens": 4},
        "provider_request_id": "req-1",
    }
    assert state["original_input"]["texts"][0]["text"] == " first "
    assert state["final_input"]["texts"][0]["text"] == "first"
    assert state["raw_output"]["indices"] == [1, 0]
    assert "hidden" not in str(state)


@pytest.mark.parametrize(
    "patch",
    [
        {"model_id": "other"},
        {"index_signature": "other"},
        {"dimensions": 8},
        {"encoding_format": "base64"},
        {"purpose": "query"},
        {"texts": [{"index": 0, "text": "only"}]},
        {"texts": [{"index": 1, "text": "second"}, {"index": 0, "text": "first"}]},
        {"texts": [{"index": 0, "text": "first"}, {"index": 0, "text": "second"}]},
        {"texts": [{"index": 0, "text": "first"}, {"index": True, "text": "second"}]},
        {"texts": [{"index": 0, "text": "first"}, {"index": 1, "text": " "}]},
    ],
)
def test_invalid_embedding_patch_stops_before_provider(patch):
    called = []

    async def patch_input(ctx, payload):
        return PatchInput(patch)

    async def invoke(texts):
        called.append(texts)
        return await vectors(texts)

    state = {}
    with pytest.raises(HookFailed):
        call(boundary(manager_for(patch_input)), invoke=invoke, state=state)
    assert called == [] and state["actual_status"] == "not_started"


def test_rejection_is_durable_and_never_calls_provider():
    called = []

    async def reject(ctx, payload):
        return Reject("private_data", "Text is not permitted")

    async def invoke(texts):
        called.append(texts)
        return await vectors(texts)

    owner, state = boundary(manager_for(reject)), {}
    for _ in range(2):
        with pytest.raises(HookRejected, match="private_data"):
            call(owner, invoke=invoke, state=state)
    assert called == []


def test_embedding_default_filters_include_documents_and_queries_only():
    seen = []

    async def observe(ctx, payload):
        seen.append(ctx.purpose)
        return Continue()

    manager = manager_for(observe)
    owner = boundary(manager)
    call(owner)
    call(owner, purpose="query")
    asyncio.run(
        manager.run(
            "model.embedding.before",
            {
                "texts": [{"index": 0, "text": "document"}],
                "model_id": "embed-1",
                "index_signature": "signature-1",
                "dimensions": None,
                "encoding_format": "float",
                "purpose": "document",
            },
            scope={"purpose": "action"},
            state={},
            save=lambda: None,
        )
    )
    assert seen == ["document", "query"]
    seen.clear()
    owner = boundary(manager_for(observe, purposes=("query",), targets=("embed-1",)))
    call(owner)
    call(owner, purpose="query")
    assert seen == ["query"]


def test_after_requires_observation_definition_and_cannot_change_vectors():
    async def patch_output(ctx, payload):
        return PatchOutput({"vectors": [[9, 9], [9, 9]]})

    with pytest.raises(ValueError, match="Observation-only"):
        manager_for(patch_output, "after")
    state = {}
    with pytest.raises(HookFailed):
        call(boundary(manager_for(patch_output, "after", observation=True)), state=state)
    assert state["raw_output"]["vectors"] == [[0.0, 1.0], [1.0, 1.0]]
    assert state["actual_status"] == "succeeded"


def test_after_failure_restores_persisted_result_without_repeating_provider():
    provider_calls, attempts, snapshots = [], [], []
    state = {}

    async def observe(ctx, payload):
        assert any(snapshot.get("raw_output") for snapshot in snapshots)
        with pytest.raises(TypeError):
            payload["vectors"][0][0] = 5
        attempts.append(ctx.operation_id)
        if len(attempts) == 1:
            raise RuntimeError("temporary telemetry failure")
        return Continue()

    async def invoke(texts):
        provider_calls.append(texts)
        return await vectors(texts)

    owner = boundary(manager_for(observe, "after", observation=True))
    with pytest.raises(HookFailed):
        call(owner, invoke=invoke, state=state, save=lambda: snapshots.append(copy.deepcopy(state)))
    state = copy.deepcopy(snapshots[-1])
    result = call(owner, invoke=invoke, state=state)
    assert result["dimension"] == 2 and len(provider_calls) == 1
    assert attempts[0] == attempts[1] and state["stage"] == "completed"


@pytest.mark.parametrize(
    "response",
    [
        {"vectors": [[0, 1]], "indices": [0]},
        {"vectors": [[0, 1], [2, 3]], "indices": [0, 0]},
        {"vectors": [[0, 1], [2, 3]], "indices": [0, True]},
        {"vectors": [[0, 1], [2]], "indices": [0, 1]},
        {"vectors": [[0, True], [2, 3]], "indices": [0, 1]},
        {"vectors": [[0, float("nan")], [2, 3]], "indices": [0, 1]},
        {"vectors": [[0, float("inf")], [2, 3]], "indices": [0, 1]},
        {"vectors": [[0, 1], [2, 3]], "indices": [0, 1], "dimension": 3},
    ],
)
def test_invalid_actual_response_is_retained_and_not_repeated(response):
    calls = []

    async def invoke(texts):
        calls.append(texts)
        return response

    state, owner = {}, boundary()
    for _ in range(2):
        with pytest.raises(RuntimeError, match="向量响应"):
            call(owner, invoke=invoke, state=state)
    assert len(calls) == 1 and "raw_output" in state
    assert state["actual_status"] == "succeeded" and state["stage"] == "after"


def test_expected_dimension_is_enforced_before_after_hook():
    seen = []

    async def observe(ctx, payload):
        seen.append(payload)
        return Continue()

    state = {}
    with pytest.raises(RuntimeError, match="维度"):
        call(boundary(manager_for(observe, "after", observation=True), dimensions=3), state=state)
    assert seen == [] and "raw_output" in state


def test_learned_dimension_is_checked_before_after_without_changing_request_identity():
    seen = []

    async def observe(ctx, payload):
        seen.append(payload)
        return Continue()

    state = {}
    owner = boundary(manager_for(observe, "after", observation=True), expected_dimensions=3)
    with pytest.raises(RuntimeError, match="维度"):
        call(owner, state=state)
    assert seen == [] and state["raw_output"]["vectors"]
    assert state["original_input"]["dimensions"] is None

    completed = {}
    call(boundary(), state=completed)
    with pytest.raises(RuntimeError, match="维度"):
        call(boundary(expected_dimensions=3), state=completed)


def test_embedding_timeout_blocks_and_observation_can_continue():
    async def slow(ctx, payload):
        await asyncio.sleep(1)
        return Continue()

    state = {}
    with pytest.raises(HookFailed):
        call(boundary(manager_for(slow, timeout=0.01)), state=state)
    assert state["actual_status"] == "not_started"
    owner = boundary(
        manager_for(slow, "after", observation=True, timeout=0.01, failure_policy="continue")
    )
    result = call(owner)
    assert result["dimension"] == 2


def test_cancelled_network_call_requires_reconciliation_without_replay():
    state, calls = {}, []
    owner = boundary()

    async def scenario():
        started = asyncio.Event()

        async def invoke(texts):
            calls.append(texts)
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(owner.invoke(["text"], invoke, state=state, save=lambda: None))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(HookRecoveryRequired):
            await owner.invoke(["text"], invoke, state=state, save=lambda: None)

    asyncio.run(scenario())
    assert len(calls) == 1 and state["actual_status"] == "unknown"


def test_recovery_refuses_different_batch_or_index_signature():
    state = {}
    owner = boundary()
    call(owner, state=state)
    with pytest.raises(HookRecoveryRequired, match="batch"):
        call(owner, ["different", "second"], state=state)
    changed = EmbeddingHookBoundary(HookManager(), model_id="embed-1", index_signature="new")
    with pytest.raises(HookRecoveryRequired, match="batch"):
        call(changed, state=state)


def test_legacy_long_url_index_signature_is_preserved():
    signature = "https://example.test/" + "long-path/" * 30 + "v1|embedding-model"
    owner = EmbeddingHookBoundary(HookManager(), model_id="embed-1", index_signature=signature)
    state = {}
    assert call(owner, state=state)["dimension"] == 2
    assert state["scope"]["index_signature"] == signature
    assert state["original_input"]["index_signature"] == signature


def test_explicit_unknown_retry_preserves_before_hooks_and_operation_attempts():
    before_calls, provider_calls = [], []

    async def clean(ctx, payload):
        before_calls.append(ctx.operation_id)
        return Continue()

    async def invoke(texts):
        provider_calls.append(texts)
        if len(provider_calls) == 1:
            raise TimeoutError()
        return await vectors(texts)

    owner, state = boundary(manager_for(clean)), {}
    with pytest.raises(TimeoutError):
        call(owner, invoke=invoke, state=state)
    with pytest.raises(HookRecoveryRequired):
        call(owner, invoke=invoke, state=state)
    result = call(owner, invoke=invoke, state=state, retry_unknown=True)
    assert result["dimension"] == 2
    assert len(before_calls) == 1 and len(provider_calls) == 2
    assert len(state["attempts"]) == 3
    with pytest.raises(ValueError, match="explicit boolean"):
        call(owner, retry_unknown="true")
