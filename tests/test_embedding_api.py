"""Knowledge APIs keep embedding preprocessing pinned and imports recoverable."""

import asyncio
import json
import time

import pytest
from agentloom import knowledge
from agentloom import store as db
from agentloom.security import decrypt, digest
from agentloom.services import hooks
from agentloom_runtime.hooks import Continue, HookDefinition, HookRegistry, PatchInput
from test_platform import add_agent, config


@pytest.fixture
def embedding_model(client, monkeypatch):
    monkeypatch.setattr(hooks, "TRUSTED_HOOKS", HookRegistry())
    response = client.post(
        "/api/models",
        json={
            "name": "Embedding test",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model_id": "embed-test",
            "api_key": "embedding-private-key",
            "purpose": "embedding",
        },
    )
    assert response.status_code == 200
    return response.json()


def install(handler, *, point="before", observation=False, config=None):
    hooks.register_trusted_hook(
        HookDefinition("embedding-api", "v1", handler, observation=observation, replay_safe=True)
    )
    return {
        "binding_id": "embedding-api",
        "hook_id": "embedding-api",
        "point": "model.embedding." + point,
        "config": config or {},
    }


def create_kb(client, model, bindings=(), **options):
    response = client.post(
        "/api/wiki",
        json={
            "name": "Embedding wiki",
            "embedding_id": model["id"],
            "embedding_hooks": list(bindings),
            **options,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def upload(client, kb, text="private architecture uses events", name="architecture.md"):
    response = client.post(f"/api/wiki/{kb['id']}/upload", files=[("files", (name, text.encode()))])
    assert response.status_code == 200, response.text
    return response.json()[0]


async def vectors(model, texts, secret):
    return {
        "vectors": [[1.0, 0.0] for _ in texts],
        "indices": list(range(len(texts))),
        "usage": {"total_tokens": len(texts)},
    }


def test_both_sides_use_frozen_hook_and_keep_original_document(
    client, embedding_model, monkeypatch
):
    seen, sent = [], []

    async def preprocess(ctx, payload):
        seen.append((ctx.purpose, ctx.kb_id, ctx.index_signature))
        return PatchInput(
            {
                "texts": [
                    {"index": entry["index"], "text": entry["text"].replace("private", "masked")}
                    for entry in payload["texts"]
                ]
            }
        )

    async def invoke(model, texts, secret):
        assert secret == "embedding-private-key"
        assert model["embedding_dimensions"] == 2
        sent.extend(texts)
        return await vectors(model, texts, secret)

    binding = install(preprocess)
    monkeypatch.setattr(knowledge, "embeddings", invoke)
    kb = create_kb(client, embedding_model, [binding], embedding_dimensions=2)
    assert kb["embedding_index"]["hooks"][0]["version"] == "v1"
    result = upload(client, kb)
    assert result["status"] == "ready" and result["import_id"]
    original = client.get(f"/api/documents/{result['id']}").json()
    assert original["content"] == "private architecture uses events"
    found = client.post(f"/api/wiki/{kb['id']}/search", json={"query": "private architecture"})
    assert found.status_code == 200 and found.json()
    assert found.json()[0]["content"] == original["content"]
    assert sent == ["masked architecture uses events", "masked architecture"]
    assert [value[0] for value in seen] == ["document", "query"]
    assert all(value[1:] == (kb["id"], kb["embedding_index"]["signature"]) for value in seen)
    metadata = client.get(f"/api/wiki/{kb['id']}/embedding-operations")
    assert metadata.status_code == 200 and len(metadata.json()) == 2
    assert "embedding-private-key" not in metadata.text
    assert "private architecture" not in metadata.text and '"vectors"' not in metadata.text
    for row in db.query("SELECT payload FROM embedding_operations"):
        assert "private architecture" not in row["payload"]
        assert "embedding-private-key" not in decrypt(row["payload"])


def test_failed_after_hook_resumes_import_without_reembedding(client, embedding_model, monkeypatch):
    after_calls, model_calls = [], []

    async def observe(ctx, payload):
        after_calls.append(ctx.operation_id)
        if len(after_calls) == 1:
            raise RuntimeError("private-hook-error-must-not-leak")
        return Continue()

    async def invoke(model, texts, secret):
        model_calls.append(texts)
        return await vectors(model, texts, secret)

    binding = install(observe, point="after", observation=True)
    monkeypatch.setattr(knowledge, "embeddings", invoke)
    kb = create_kb(client, embedding_model, [binding])
    failed = upload(client, kb)
    assert failed["status"] == "failed" and failed["import_id"]
    assert failed["operation_id"] and not failed["retry_required"]
    assert "private-hook-error" not in json.dumps(failed)
    assert client.get(f"/api/wiki/{kb['id']}/documents").json() == []
    rows = db.query("SELECT payload FROM embedding_operations")
    assert len(rows) == 1 and json.loads(decrypt(rows[0]["payload"]))["state"]["raw_output"]
    imports = client.get(f"/api/wiki/{kb['id']}/imports").json()
    assert len(imports) == 1 and imports[0]["import_id"] == failed["import_id"]
    resumed = client.post(f"/api/wiki/{kb['id']}/imports/{failed['import_id']}/resume", json={})
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == "ready"
    assert len(model_calls) == 1 and after_calls[0] == after_calls[1]
    documents = client.get(f"/api/wiki/{kb['id']}/documents").json()
    assert len(documents) == 1
    again = client.post(f"/api/wiki/{kb['id']}/imports/{failed['import_id']}/resume", json={})
    assert again.status_code == 200 and len(model_calls) == 1
    assert len(client.get(f"/api/wiki/{kb['id']}/documents").json()) == 1


def test_unknown_query_requires_explicit_retry_and_operation_identity(
    client, embedding_model, monkeypatch
):
    query_calls = []

    async def invoke(model, texts, secret):
        if texts == ["question"]:
            query_calls.append(texts)
            if len(query_calls) == 1:
                raise TimeoutError("embedding-private-key")
        return await vectors(model, texts, secret)

    monkeypatch.setattr(knowledge, "embeddings", invoke)
    kb = create_kb(client, embedding_model)
    assert upload(client, kb)["status"] == "ready"
    route = f"/api/wiki/{kb['id']}/search"
    first = client.post(route, json={"query": "question"})
    assert first.status_code == 409 and first.json()["retry_required"]
    operation_id = first.json()["operation_id"]
    assert "embedding-private-key" not in first.text
    payload = {"query": "question", "embedding_operation_id": operation_id}
    again = client.post(route, json=payload)
    assert again.status_code == 409 and len(query_calls) == 1
    changed = client.post(route, json={**payload, "query": "different"})
    assert changed.status_code in (400, 409, 422) and len(query_calls) == 1
    invalid = client.post(route, json={**payload, "retry_unknown_models": "true"})
    assert invalid.status_code == 422
    resumed = client.post(route, json={**payload, "retry_unknown_models": True})
    assert resumed.status_code == 200 and resumed.json()
    assert len(query_calls) == 2


def test_same_model_different_knowledge_hooks_never_share_query_projection(
    client, embedding_model, monkeypatch
):
    sent = []

    async def prefix(ctx, payload):
        return PatchInput(
            {
                "texts": [
                    {"index": row["index"], "text": ctx.config["prefix"] + row["text"]}
                    for row in payload["texts"]
                ]
            }
        )

    binding = install(prefix, config={"prefix": "A:"})

    async def invoke(model, texts, secret):
        sent.extend(texts)
        return await vectors(model, texts, secret)

    monkeypatch.setattr(knowledge, "embeddings", invoke)
    first = create_kb(client, embedding_model, [binding])
    second = create_kb(client, embedding_model, [{**binding, "config": {"prefix": "B:"}}])
    assert upload(client, first)["status"] == upload(client, second)["status"] == "ready"
    assert first["embedding_index"]["signature"] != second["embedding_index"]["signature"]
    sent.clear()
    space = db.query("SELECT id FROM spaces", one=True)["id"]
    libraries = [db.resource(item["id"], space, "wiki") for item in (first, second)]
    found = asyncio.run(knowledge.search(space, libraries, "question"))
    assert found and sorted(sent) == ["A:question", "B:question"]


def test_reindex_creates_new_resource_and_keeps_published_source(
    client, embedding_model, model, monkeypatch
):
    async def prefix(ctx, payload):
        return PatchInput(
            {
                "texts": [
                    {"index": row["index"], "text": ctx.config["prefix"] + row["text"]}
                    for row in payload["texts"]
                ]
            }
        )

    binding = install(prefix, config={"prefix": "v1:"})
    monkeypatch.setattr(knowledge, "embeddings", vectors)
    source = create_kb(client, embedding_model, [binding])
    uploaded = upload(client, source)
    assert uploaded["status"] == "ready"
    agent = add_agent(client, model, wiki=[source["id"]])
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    published = client.get(f"/api/agents/{agent['id']}/versions/1").json()
    response = client.post(
        f"/api/wiki/{source['id']}/reindex",
        json={
            "name": "New embedding index",
            "embedding_hooks": [{**binding, "config": {"prefix": "v2:"}}],
        },
    )
    assert response.status_code == 200, response.text
    rebuilt = response.json()
    assert rebuilt["id"] != source["id"] and rebuilt["status"] == "ready"
    libraries = {row["id"]: row for row in client.get("/api/resources/wiki").json()}
    current = libraries[rebuilt["id"]]
    assert current["derived_from"] == source["id"]
    assert current["embedding_index"]["revision"] == 2
    assert libraries[source["id"]]["embedding_index"] == source["embedding_index"]
    assert current["embedding_index"]["signature"] != source["embedding_index"]["signature"]
    assert client.get(f"/api/agents/{agent['id']}/versions/1").json() == published
    documents = client.get(f"/api/wiki/{rebuilt['id']}/documents").json()
    assert len(documents) == 1 and documents[0]["id"] != uploaded["id"]
    assert (
        client.get(f"/api/documents/{documents[0]['id']}").json()["content"]
        == "private architecture uses events"
    )


def test_deployed_code_drift_is_blocked_and_agent_cannot_override_embedding_chain(
    client, embedding_model, model, monkeypatch
):
    called = []

    async def original(ctx, payload):
        return Continue()

    async def changed(ctx, payload):
        return PatchInput({"texts": []})

    async def invoke(model, texts, secret):
        called.append(texts)
        return await vectors(model, texts, secret)

    binding = install(original)
    kb = create_kb(client, embedding_model, [binding])
    monkeypatch.setattr(knowledge, "embeddings", invoke)
    rejected = client.post("/api/agents", json=config(model, hooks=[binding]))
    assert rejected.status_code == 422
    monkeypatch.setattr(hooks, "TRUSTED_HOOKS", HookRegistry())
    install(changed)
    result = upload(client, kb)
    assert result["status"] == "failed" and called == []
    assert client.get(f"/api/wiki/{kb['id']}/documents").json() == []


def test_partial_reindex_stays_unpublishable_until_recovered(
    client, embedding_model, model, monkeypatch
):
    provider_calls, after_calls = [], []

    async def invoke(model, texts, secret):
        provider_calls.append(texts)
        return await vectors(model, texts, secret)

    async def observe(ctx, payload):
        after_calls.append(ctx.operation_id)
        if len(after_calls) == 1:
            raise RuntimeError("once")
        return Continue()

    monkeypatch.setattr(knowledge, "embeddings", invoke)
    source = create_kb(client, embedding_model)
    assert upload(client, source, name="first.md")["status"] == "ready"
    assert upload(client, source, text="second document", name="second.md")["status"] == "ready"
    binding = install(observe, point="after", observation=True)
    response = client.post(f"/api/wiki/{source['id']}/reindex", json={"embedding_hooks": [binding]})
    assert response.status_code == 200, response.text
    rebuilt = response.json()
    assert rebuilt["status"] != "ready"
    assert any(item["status"] == "failed" for item in rebuilt["imports"])
    agent = add_agent(client, model, wiki=[rebuilt["id"]])
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 400
    calls_before_resume = len(provider_calls)
    recovered = client.post(f"/api/wiki/{rebuilt['id']}/reindex/resume", json={})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["status"] == "ready"
    assert len(provider_calls) == calls_before_resume
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    assert len(client.get(f"/api/wiki/{source['id']}/documents").json()) == 2
    assert len(client.get(f"/api/wiki/{rebuilt['id']}/documents").json()) == 2


def other_actor_headers(space):
    actor, token = db.uid(), db.uid()
    db.execute("INSERT INTO users VALUES(?,?,?,?)", (actor, actor + "@example.test", "Other", "x"))
    db.execute("INSERT INTO members VALUES(?,?,?)", (actor, space, "member"))
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest(token), actor, space, "api", "test-other", time.time() + 3600),
    )
    return {"Authorization": "Bearer " + token}


def test_import_and_query_recovery_require_original_actor_and_space(
    client, embedding_model, monkeypatch
):
    calls, after_calls = [], []

    async def invoke(model, texts, secret):
        calls.append(texts)
        if texts == ["query unknown"]:
            raise TimeoutError("private-provider-error")
        return await vectors(model, texts, secret)

    async def observe(ctx, payload):
        after_calls.append(ctx.operation_id)
        if len(after_calls) == 1:
            raise RuntimeError("private-hook-error")
        return Continue()

    monkeypatch.setattr(knowledge, "embeddings", invoke)
    binding = install(observe, point="after", observation=True)
    kb = create_kb(client, embedding_model, [binding])
    failed = upload(client, kb)
    space = db.query("SELECT id FROM spaces", one=True)["id"]
    same_space = other_actor_headers(space)
    other_space = other_actor_headers("foreign-space")
    route = f"/api/wiki/{kb['id']}/imports/{failed['import_id']}/resume"
    for headers in (same_space, other_space):
        assert client.get("/api/resources/wiki", headers=headers).status_code == 200
        denied = client.post(route, json={}, headers=headers)
        assert denied.status_code in (400, 403, 404)
    assert len(after_calls) == len(calls) == 1
    assert client.post(route, json={}).json()["status"] == "ready"
    search_route = f"/api/wiki/{kb['id']}/search"
    unknown = client.post(search_route, json={"query": "query unknown"})
    assert unknown.status_code == 409
    resume = {
        "query": "query unknown",
        "embedding_operation_id": unknown.json()["operation_id"],
        "retry_unknown_models": True,
    }
    before = len(calls)
    for headers in (same_space, other_space):
        assert client.get("/api/resources/wiki", headers=headers).status_code == 200
        denied = client.post(search_route, json=resume, headers=headers)
        assert denied.status_code in (400, 403, 404)
        assert "private-provider-error" not in denied.text
    assert len(calls) == before
