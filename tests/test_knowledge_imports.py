"""Whole-file imports commit atomically and recover each persisted model batch."""

import asyncio
import json

import pytest
from agentloom import knowledge
from agentloom import store as db
from agentloom.security import decrypt
from agentloom.services import embedding
from agentloom.services import knowledge_imports as imports
from agentloom_runtime.hooks import Continue, PatchInput
from agentloom_runtime.tool_contracts import ToolPersistenceError
from test_embedding_service import context as context
from test_embedding_service import freeze, install


def long_document(count=65):
    return "\n".join(f"private-source-{number}: " + "x" * 1000 for number in range(count))


def empty_documents(space, kb):
    assert (
        db.query("SELECT id FROM documents WHERE kb_id=? AND space_id=?", (kb["id"], space)) == []
    )
    assert db.query("SELECT id FROM chunks WHERE kb_id=? AND space_id=?", (kb["id"], space)) == []


def test_multi_batch_after_failure_keeps_atomic_original_document_and_recovers(context):
    seen = []
    calls = []
    after_calls = []

    async def before(ctx, payload):
        seen.append(ctx.operation_id)
        return PatchInput(
            {
                "texts": [
                    {
                        "index": value["index"],
                        "text": value["text"].replace("private-source", "masked-source"),
                    }
                    for value in payload["texts"]
                ]
            }
        )

    before_binding = install("model.embedding.before", before)

    async def after(ctx, payload):
        after_calls.append(ctx.operation_id)
        if len(after_calls) == 2:
            raise RuntimeError("observer temporary error")
        return Continue()

    # A separate ID is needed for the second deployed definition.
    from agentloom.services import hooks
    from agentloom_runtime.hooks import HookDefinition

    hooks.register_trusted_hook(
        HookDefinition("after-import", "v1", after, observation=True, replay_safe=True)
    )
    after_binding = {
        "binding_id": "after-import",
        "hook_id": "after-import",
        "point": "model.embedding.after",
    }
    space, actor, _, _ = context
    kb = freeze(context, [before_binding, after_binding])
    source = long_document()
    assert len(list(knowledge.split_text(source))) == 65

    async def invoke(model, texts, secret):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    import_id = imports.create_import(space, kb, "source.md", source, actor)
    with pytest.raises(imports.ImportFailure) as failure:
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke))
    assert not failure.value.retry_required
    assert [len(batch) for batch in calls] == [32, 32]
    empty_documents(space, kb)
    assert imports.get_import(space, kb["id"], import_id, actor)["operation_id"] == after_calls[-1]
    result = asyncio.run(imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke))
    assert result["status"] == "ready" and result["id"]
    assert [len(batch) for batch in calls] == [32, 32, 1]
    assert len(seen) == 3 and after_calls[1] == after_calls[2]
    assert all("masked-source" in text for batch in calls for text in batch)
    document = db.query("SELECT * FROM documents WHERE id=?", (result["id"],), True)
    assert document["content"] == source
    chunks = db.query("SELECT * FROM chunks WHERE document_id=?", (result["id"],))
    assert len(chunks) == 65 and all("private-source" in chunk["content"] for chunk in chunks)
    assert db.resource(kb["id"], space)["embedding_dimension"] == 2
    assert (
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke))
        == result
    )
    assert len(calls) == 3 and db.resource(kb["id"], space)["revision"] == 1
    ciphertext = db.query("SELECT payload FROM knowledge_imports WHERE id=?", (import_id,), True)[
        "payload"
    ]
    assert "private-source" not in ciphertext and "private-source" in decrypt(ciphertext)
    assert "private-source" not in json.dumps(result)


def test_cancellation_and_lost_import_failure_metadata_require_unknown_consent(
    context, monkeypatch
):
    space, actor, _, _ = context
    kb = freeze(context)
    import_id = imports.create_import(space, kb, "source.md", long_document(33), actor)
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def invoke(model, texts, secret):
            calls.append(texts)
            if len(calls) == 2:
                entered.set()
                await asyncio.Event().wait()
            return [[1.0, 0.0] for _ in texts]

        task = asyncio.create_task(
            imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke)
        )
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    empty_documents(space, kb)
    metadata = imports.get_import(space, kb["id"], import_id, actor)
    assert metadata["status"] == "interrupted" and metadata["retry_required"]
    operation_id = metadata["operation_id"]
    assert operation_id
    # Process loss may leave only the batch checkpoint and a 'running' import.
    db.execute("UPDATE knowledge_imports SET status='running' WHERE id=?", (import_id,))
    assert imports.list_imports(space, kb["id"], actor)[0]["retry_required"]
    actual_embed = embedding.embed
    attempted = []

    async def tracking(*args, **kwargs):
        attempted.append(True)
        return await actual_embed(*args, **kwargs)

    monkeypatch.setattr(embedding, "embed", tracking)
    with pytest.raises(imports.ImportFailure) as failure:
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor))
    assert failure.value.operation_id == operation_id and failure.value.retry_required
    assert attempted == []

    async def retry(model, texts, secret):
        calls.append(texts)
        return [[1.0, 0.0] for _ in texts]

    result = asyncio.run(
        imports.execute_import(space, kb["id"], import_id, actor, retry_unknown=True, invoke=retry)
    )
    assert result["status"] == "ready" and not result["retry_required"]
    assert [len(batch) for batch in calls] == [32, 1, 1]


def test_unexpected_errors_are_sanitized_and_resume_uses_all_saved_vectors(context, monkeypatch):
    space, actor, _, _ = context
    kb = freeze(context)
    import_id = imports.create_import(space, kb, "source.md", "original source", actor)
    actual_commit = imports._commit
    calls = []

    async def invoke(model, texts, secret):
        calls.append(True)
        return [[1.0, 0.0]]

    def fail(*args):
        raise RuntimeError("password=secret-source-contents")

    monkeypatch.setattr(imports, "_commit", fail)
    with pytest.raises(imports.ImportFailure) as failure:
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke))
    assert "secret-source" not in str(failure.value)
    assert "secret-source" not in json.dumps(imports.get_import(space, kb["id"], import_id, actor))
    empty_documents(space, kb)
    monkeypatch.setattr(imports, "_commit", actual_commit)
    assert (
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor, invoke=invoke))[
            "status"
        ]
        == "ready"
    )
    assert calls == [True]


def test_atomic_commit_rejects_inconsistent_batch_dimensions(context):
    space, actor, _, _ = context
    kb = freeze(context)
    calls = []

    async def invoke(model, texts, secret):
        calls.append(True)
        return [[1.0] * (2 if len(calls) == 1 else 3) for _ in texts]

    with pytest.raises(imports.ImportFailure):
        asyncio.run(
            imports.import_document(space, kb, "source.md", long_document(33), actor, invoke=invoke)
        )
    empty_documents(space, kb)
    assert db.resource(kb["id"], space).get("embedding_dimension") is None


def test_owner_membership_and_frozen_index_are_checked_before_any_provider(context):
    space, actor, model, _ = context
    kb = freeze(context)
    import_id = imports.create_import(space, kb, "source.md", "original", actor)
    db.execute("INSERT INTO members VALUES(?,?,?)", ("other", space, "member"))
    with pytest.raises(ValueError, match="无权访问"):
        imports.get_import(space, kb["id"], import_id, "other")
    changed = embedding.freeze_index(space, model["id"], revision=2)
    db.put_resource(space, "wiki", {**kb, "embedding_index": changed}, kb["id"])
    with pytest.raises(imports.ImportFailure):
        asyncio.run(imports.execute_import(space, kb["id"], import_id, actor))
    assert db.query("SELECT id FROM embedding_operations") == []
    db.execute("DELETE FROM members WHERE user_id=? AND space_id=?", (actor, space))
    with pytest.raises(ValueError, match="无权访问"):
        imports.list_imports(space, kb["id"], actor)


@pytest.mark.parametrize("error_type", [RuntimeError, ValueError])
def test_rebuild_creation_is_atomic_and_preserves_full_original_source(
    context, monkeypatch, error_type
):
    space, actor, model, kb = context
    # Populate source without embeddings so source data cannot be reconstructed
    # accidentally from transformed provider inputs or filtered model history.
    source = db.put_resource(space, "wiki", {"name": "source", "embedding_id": ""}, kb["id"])
    for name in ("a.md", "b.sql"):
        asyncio.run(imports.import_document(space, source, name, "original " + name, actor))
    new_index = embedding.freeze_index(space, model["id"], revision=2)
    target_payload = {"name": "rebuilt", "embedding_id": model["id"], "embedding_index": new_index}
    resources_before = len(db.list_resources(space, "wiki"))
    jobs_before = len(db.query("SELECT id FROM knowledge_imports"))
    actual_encrypt = imports.encrypt
    writes = []

    def fail_second(value):
        writes.append(True)
        if len(writes) == 2:
            raise error_type("sensitive database failure")
        return actual_encrypt(value)

    monkeypatch.setattr(imports, "encrypt", fail_second)
    with pytest.raises(ToolPersistenceError):
        imports.prepare_rebuild(space, source, target_payload, actor)
    assert len(db.list_resources(space, "wiki")) == resources_before
    assert len(db.query("SELECT id FROM knowledge_imports")) == jobs_before
    monkeypatch.setattr(imports, "encrypt", actual_encrypt)
    target = imports.prepare_rebuild(space, source, target_payload, actor)
    assert target["id"] != source["id"] and target["derived_from"] == source["id"]
    assert target["status"] == "building" and len(target["rebuild_imports"]) == 2
    records = db.query(
        "SELECT payload FROM knowledge_imports WHERE kb_id=? ORDER BY created,id", (target["id"],)
    )
    assert sorted(json.loads(decrypt(record["payload"]))["text"] for record in records) == [
        "original a.md",
        "original b.sql",
    ]
    with pytest.raises(ValueError, match="完整构建"):
        imports.prepare_rebuild(space, target, target_payload, actor)

    async def invoke(model, texts, secret):
        return [[1.0, 0.0] for _ in texts]

    for index, import_id in enumerate(target["rebuild_imports"]):
        asyncio.run(imports.execute_import(space, target["id"], import_id, actor, invoke=invoke))
        assert db.resource(target["id"], space)["status"] == ("building" if index == 0 else "ready")
    assert len(db.query("SELECT id FROM documents WHERE kb_id=?", (source["id"],))) == 2
    assert len(db.query("SELECT id FROM documents WHERE kb_id=?", (target["id"],))) == 2


def test_empty_source_rebuild_does_not_create_resource(context):
    space, actor, _, source = context
    before = db.list_resources(space, "wiki")
    with pytest.raises(ValueError, match="没有可重建"):
        imports.prepare_rebuild(space, source, {"name": "new", "embedding_id": ""}, actor)
    assert db.list_resources(space, "wiki") == before
