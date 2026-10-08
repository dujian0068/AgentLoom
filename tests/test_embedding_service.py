"""Pinned embedding indexes and durable, scoped operation recovery."""

import asyncio
import json

import pytest
from agentloom import store as db
from agentloom.security import decrypt, encrypt
from agentloom.services import embedding as service
from agentloom.services import hooks
from agentloom_runtime.hooks import Continue, HookDefinition, HookRegistry, PatchInput
from agentloom_runtime.tool_contracts import ToolPersistenceError


@pytest.fixture
def context(client, monkeypatch):
    monkeypatch.setattr(hooks, "TRUSTED_HOOKS", HookRegistry())
    space = db.query("SELECT id FROM spaces", one=True)["id"]
    actor = db.query("SELECT id FROM users", one=True)["id"]
    model = db.put_resource(
        space,
        "models",
        {
            "name": "Embedding",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model_id": "embed-test",
            "secret": encrypt("embedding-secret"),
            "purpose": "embedding",
        },
    )
    kb = db.put_resource(space, "wiki", {"name": "Wiki", "embedding_id": model["id"]})
    return space, actor, model, kb


def install(point, handler, *, observation=False, replay_safe=True, version="v1"):
    definition = HookDefinition(
        "embedding-test", version, handler, observation=observation, replay_safe=replay_safe
    )
    hooks.register_trusted_hook(definition)
    return {"binding_id": "embedding-binding", "hook_id": "embedding-test", "point": point}


def freeze(context, binding=(), dimensions=None):
    space, _, model, kb = context
    index = service.freeze_index(space, model["id"], binding, dimensions)
    kb = db.put_resource(space, "wiki", {**kb, "embedding_index": index}, kb["id"])
    return kb


async def vectors(model, texts, secret):
    return [[1.0, 0.0] for _ in texts]


def test_freeze_pins_code_identity_dimensions_but_allows_key_rotation(context):
    async def before(ctx, payload):
        return Continue()

    space, _, model, kb = context
    binding = install("model.embedding.before", before)
    kb = freeze(context, [binding], 2)
    index = kb["embedding_index"]
    assert index["signature"].startswith("sha256:")
    assert index["hooks"][0]["version"] == "v1"
    assert "embedding-secret" not in json.dumps(index)
    assert "secret" not in index["model"]
    db.put_resource(space, "models", {**model, "secret": encrypt("rotated")}, model["id"])
    resolved, current, _ = service.resolve_index(space, kb)
    assert resolved == index and decrypt(current["secret"]) == "rotated"
    # A newly deployed version cannot change the pinned v1 Hook.
    hooks.register_trusted_hook(HookDefinition("embedding-test", "v2", before, replay_safe=True))
    assert service.resolve_index(space, kb)[0] == index
    db.put_resource(space, "models", {**model, "model_id": "different"}, model["id"])
    with pytest.raises(ValueError, match="模型连接已变化"):
        service.resolve_index(space, kb)


def test_deployed_hook_code_drift_or_index_mutation_is_rejected(context, monkeypatch):
    async def before(ctx, payload):
        return Continue()

    async def changed(ctx, payload):
        return PatchInput({"texts": []})

    space, _, _, _ = context
    kb = freeze(context, [install("model.embedding.before", before)])
    monkeypatch.setattr(hooks, "TRUSTED_HOOKS", HookRegistry())
    install("model.embedding.before", changed)
    with pytest.raises(ValueError, match="代码或配置已变化"):
        service.resolve_index(space, kb)
    kb["embedding_index"]["dimensions"] = 3
    with pytest.raises(ValueError, match="配置损坏"):
        service.resolve_index(space, kb)


@pytest.mark.parametrize(
    "changes",
    [
        {"point": "model.chat.before"},
        {"purposes": ["query"]},
        {"purposes": ["action"]},
        {"instances": ["child"]},
        {"targets": ["different-model"]},
    ],
)
def test_both_sides_must_use_the_same_preprocessing_chain(context, changes):
    async def before(ctx, payload):
        return Continue()

    space, _, model, _ = context
    binding = install("model.embedding.before", before)
    with pytest.raises(ValueError):
        service.freeze_index(space, model["id"], [{**binding, **changes}])


def test_legacy_index_keeps_existing_vector_signature_without_write(context):
    space, _, _, kb = context
    before = db.resource(kb["id"], space)
    index, _, manager = service.resolve_index(space, kb)
    assert index["signature"] == "https://example.test/v1|embed-test"
    assert index["legacy"] and manager.checkpoint_config()["bindings"] == []
    assert db.resource(kb["id"], space) == before
    assert service.index_signature(space, kb) == index["signature"]


def test_rebuild_revision_changes_signature_and_helper_avoids_executor(context, monkeypatch):
    space, _, model, kb = context
    first = service.freeze_index(space, model["id"])
    second = service.freeze_index(space, model["id"], revision=2)
    assert first["signature"] != second["signature"]
    kb = db.put_resource(space, "wiki", {**kb, "embedding_index": second}, kb["id"])
    assert service.resolve_index(space, kb)[0]["revision"] == 2

    def forbidden(*args):
        raise AssertionError("signature read must not construct an executor")

    monkeypatch.setattr(service, "hook_manager", forbidden)
    assert service.index_signature(space, kb) == second["signature"]
    for revision in (0, -1, True, "2"):
        with pytest.raises(ValueError, match="正整数"):
            service.freeze_index(space, model["id"], revision=revision)


def test_patches_both_document_and_query_and_encrypts_operations(context):
    seen = []
    provider_calls = []

    async def before(ctx, payload):
        seen.append((ctx.purpose, ctx.kb_id, ctx.actor_id, ctx.index_signature))
        return PatchInput(
            {
                "texts": [
                    {"index": item["index"], "text": item["text"].replace("private", "masked")}
                    for item in payload["texts"]
                ]
            }
        )

    async def invoke(model, texts, secret):
        provider_calls.append((texts, secret, model["embedding_dimensions"]))
        return {
            "vectors": [[1.0, 0.0] for _ in texts],
            "indices": list(range(len(texts))),
            "usage": {"total_tokens": 2},
        }

    space, actor, _, _ = context
    kb = freeze(context, [install("model.embedding.before", before)], 2)
    for purpose in ("document", "query"):
        value, oid = asyncio.run(
            service.embed(
                space, kb, ["private material"], purpose=purpose, actor_id=actor, invoke=invoke
            )
        )
        assert value == [[1.0, 0.0]]
        stored = db.query("SELECT payload FROM embedding_operations WHERE id=?", (oid,), True)[
            "payload"
        ]
        assert "private" not in stored and "masked" not in stored
        plain = decrypt(stored)
        assert "private material" in plain and "masked material" in plain
        assert "embedding-secret" not in plain
        metadata = service.get_operation(space, oid, actor)
        assert metadata["status"] == "succeeded" and metadata["dimensions"] == 2
        assert "private" not in json.dumps(metadata) and "vectors" not in metadata
    assert [value[0] for value in seen] == ["document", "query"]
    assert all(value[1] == kb["id"] and value[2] == actor for value in seen)
    assert provider_calls == [(["masked material"], "embedding-secret", 2)] * 2


def test_after_failure_resumes_stored_result_without_provider_repeat(context):
    after_calls = []
    actual_calls = []

    async def after(ctx, payload):
        after_calls.append(ctx.operation_id)
        if len(after_calls) == 1:
            raise RuntimeError("private error")
        return Continue()

    async def invoke(model, texts, secret):
        actual_calls.append(texts)
        return [[1.0, 0.0]]

    space, actor, _, _ = context
    kb = freeze(context, [install("model.embedding.after", after, observation=True)])
    with pytest.raises(service.EmbeddingOperationError) as failure:
        asyncio.run(
            service.embed(space, kb, ["text"], purpose="document", actor_id=actor, invoke=invoke)
        )
    error = failure.value
    assert not error.retry_required and "private error" not in str(error)
    output, metadata = asyncio.run(
        service.resume_operation(space, error.operation_id, actor, invoke=invoke)
    )
    assert output == [[1.0, 0.0]] and metadata["status"] == "succeeded"
    assert len(actual_calls) == 1 and len(after_calls) == 2


def test_unknown_call_requires_explicit_optin_and_keeps_before_hook_state(context):
    actual_calls, hook_calls = [], []

    async def before(ctx, payload):
        hook_calls.append(True)
        return Continue()

    async def invoke(model, texts, secret):
        actual_calls.append(True)
        if len(actual_calls) == 1:
            raise RuntimeError("provider secret must not escape")
        return [[1.0, 0.0]]

    space, actor, _, _ = context
    kb = freeze(context, [install("model.embedding.before", before, replay_safe=False)])
    with pytest.raises(service.EmbeddingOperationError) as failure:
        asyncio.run(
            service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=invoke)
        )
    oid = failure.value.operation_id
    assert failure.value.retry_required
    with pytest.raises(service.EmbeddingOperationError):
        asyncio.run(service.resume_operation(space, oid, actor, invoke=invoke))
    assert len(actual_calls) == 1
    output, metadata = asyncio.run(
        service.resume_operation(space, oid, actor, retry_unknown=True, invoke=invoke)
    )
    assert output == [[1.0, 0.0]] and metadata["status"] == "succeeded"
    assert len(actual_calls) == 2 and len(hook_calls) == 1


def test_operation_scope_and_input_are_immutable(context):
    space, actor, _, _ = context
    kb = freeze(context)
    _, oid = asyncio.run(
        service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=vectors)
    )
    with pytest.raises(ValueError, match="输入、身份或索引配置"):
        asyncio.run(
            service.embed(
                space,
                kb,
                ["changed"],
                purpose="query",
                actor_id=actor,
                operation_id=oid,
                invoke=vectors,
            )
        )
    with pytest.raises(ValueError, match="无权访问"):
        service.get_operation("other-space", oid)
    db.execute("INSERT INTO members VALUES(?,?,?)", ("other-user", space, "member"))
    with pytest.raises(ValueError, match="无权访问"):
        service.get_operation(space, oid, "other-user")
    db.execute("DELETE FROM members WHERE user_id=? AND space_id=?", (actor, space))
    with pytest.raises(ValueError, match="无权访问"):
        asyncio.run(service.resume_operation(space, oid, actor, invoke=vectors))


def test_same_operation_cannot_dispatch_concurrently(context):
    space, actor, _, _ = context
    kb = freeze(context)
    calls = []

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def invoke(model, texts, secret):
            calls.append(True)
            entered.set()
            await release.wait()
            return [[1.0, 0.0]]

        operation_id = db.uid()
        first = asyncio.create_task(
            service.embed(
                space,
                kb,
                ["text"],
                purpose="query",
                actor_id=actor,
                operation_id=operation_id,
                invoke=invoke,
            )
        )
        await entered.wait()
        try:
            with pytest.raises(service.EmbeddingOperationError) as failure:
                await service.embed(
                    space,
                    kb,
                    ["text"],
                    purpose="query",
                    actor_id=actor,
                    operation_id=operation_id,
                    invoke=invoke,
                )
            assert failure.value.status == "busy"
        finally:
            release.set()
            await first

    asyncio.run(scenario())
    assert calls == [True]


def test_persistence_failure_stops_before_provider(context, monkeypatch):
    space, actor, _, _ = context
    kb = freeze(context)
    calls = []

    def fail(*args):
        raise ToolPersistenceError("disk unavailable")

    async def invoke(*args):
        calls.append(True)
        return [[1.0, 0.0]]

    monkeypatch.setattr(service, "_write", fail)
    with pytest.raises(ToolPersistenceError):
        asyncio.run(
            service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=invoke)
        )
    assert calls == []


def test_explicit_provider_retry_never_replays_an_unsafe_failed_hook(context):
    hook_calls, provider_calls = [], []

    async def after(ctx, payload):
        hook_calls.append(True)
        raise RuntimeError("external logging outcome unknown")

    async def invoke(model, texts, secret):
        provider_calls.append(True)
        return [[1.0, 0.0]]

    space, actor, _, _ = context
    kb = freeze(
        context,
        [install("model.embedding.after", after, observation=True, replay_safe=False)],
    )
    with pytest.raises(service.EmbeddingOperationError) as failure:
        asyncio.run(
            service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=invoke)
        )
    with pytest.raises(service.EmbeddingOperationError) as recovery:
        asyncio.run(
            service.resume_operation(
                space, failure.value.operation_id, actor, retry_unknown=True, invoke=invoke
            )
        )
    assert recovery.value.status == "recovery_required"
    assert not recovery.value.retry_required
    assert hook_calls == provider_calls == [True]


def test_cancellation_persists_unknown_fact_and_propagates(context):
    space, actor, _, _ = context
    kb = freeze(context)
    operation_id = db.uid()
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def invoke(model, texts, secret):
            calls.append(True)
            entered.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            service.embed(
                space,
                kb,
                ["text"],
                purpose="query",
                actor_id=actor,
                operation_id=operation_id,
                invoke=invoke,
            )
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    metadata = service.get_operation(space, operation_id, actor)
    assert metadata["status"] == "cancelled" and metadata["retry_required"]
    with pytest.raises(service.EmbeddingOperationError):
        asyncio.run(service.resume_operation(space, operation_id, actor, invoke=vectors))
    assert calls == [True]


def test_resume_rechecks_deployment_model_and_resource_access(context):
    space, actor, model, _ = context
    kb = freeze(context)
    _, oid = asyncio.run(
        service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=vectors)
    )
    db.put_resource(space, "models", {**model, "base_url": "https://changed.test/v1"}, model["id"])
    with pytest.raises(ValueError, match="模型连接已变化"):
        asyncio.run(service.resume_operation(space, oid, actor, invoke=vectors))
    db.put_resource(space, "models", model, model["id"])
    db.execute("DELETE FROM resources WHERE id=?", (kb["id"],))
    with pytest.raises(ValueError, match="无权访问"):
        asyncio.run(service.resume_operation(space, oid, actor, invoke=vectors))


def test_learned_dimension_checked_before_after_hook_and_on_completed_resume(context):
    after_calls = []

    async def after(ctx, payload):
        after_calls.append(True)
        return Continue()

    space, actor, _, _ = context
    kb = freeze(context, [install("model.embedding.after", after, observation=True)])
    _, completed = asyncio.run(
        service.embed(space, kb, ["text"], purpose="query", actor_id=actor, invoke=vectors)
    )
    assert after_calls == [True]
    kb = db.put_resource(space, "wiki", {**kb, "embedding_dimension": 3}, kb["id"])
    with pytest.raises(service.EmbeddingOperationError):
        asyncio.run(service.resume_operation(space, completed, actor, invoke=vectors))
    with pytest.raises(service.EmbeddingOperationError):
        asyncio.run(
            service.embed(space, kb, ["new text"], purpose="query", actor_id=actor, invoke=vectors)
        )
    assert after_calls == [True]
