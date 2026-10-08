"""RAG recovery belongs to the logical tool invocation, not query-text matching."""

import asyncio
import copy

import pytest
from agentloom import knowledge
from agentloom import store as db
from agentloom.services.knowledge_search import RunKnowledgeSearch, run_knowledge_search
from agentloom_runtime import provider
from agentloom_runtime.hooks import Continue, HookRecoveryRequired
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import ToolPersistenceError
from test_embedding_api import create_kb, install, upload, vectors
from test_embedding_api import embedding_model as embedding_model
from test_harness import answer, call, decision, snapshot
from test_hook_recovery_api import checkpoint
from test_platform import add_agent, wait_run


def start(client, model, libraries):
    agent = add_agent(client, model, wiki=[library["id"] for library in libraries])
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/agents/{agent['id']}/runs", json={"input": "search knowledge"})
    assert response.status_code == 200, response.text
    return response.json()["run_id"]


async def search_once(model, messages, tools, secret):
    if not tools:
        return decision()
    if messages[-1]["role"] == "tool":
        return answer("found evidence")
    return call("knowledge_search", {"query": "question"})


def test_completed_first_kb_and_failed_second_kb_resume_without_reembedding(
    client, model, embedding_model, monkeypatch
):
    after_calls, embedding_calls = [], []

    async def observe(ctx, payload):
        if ctx.purpose == "query":
            after_calls.append((ctx.kb_id, ctx.operation_id))
            if len(after_calls) == 2:
                raise RuntimeError("temporary output failure")
        return Continue()

    async def embed(model, texts, secret):
        if texts == ["question"]:
            embedding_calls.append(texts)
        return await vectors(model, texts, secret)

    binding = install(observe, point="after", observation=True)
    monkeypatch.setattr(knowledge, "embeddings", embed)
    monkeypatch.setattr(provider, "chat", search_once)
    libraries = [create_kb(client, embedding_model, [binding]) for _ in range(2)]
    for library in libraries:
        assert upload(client, library)["status"] == "ready"
    rid = start(client, model, libraries)
    state = wait_run(client, rid)
    assert state["status"] == "failed" and state["resumable"]
    assert not state["requires_model_retry"]
    pending = checkpoint(rid)["frames"]["main"]["pending"]
    operations = pending["handler_state"]["knowledge_search"]["operations"]
    assert set(operations) == {library["id"] for library in libraries}
    assert len(embedding_calls) == 2
    response = client.post(f"/api/v1/runs/{rid}/resume", json={"stream": True})
    assert response.status_code == 200 and "run.completed" in response.text
    assert wait_run(client, rid)["status"] == "succeeded"
    assert len(embedding_calls) == 2
    assert len(after_calls) == 3 and after_calls[-1] == after_calls[-2]
    records = db.query("SELECT id,status FROM embedding_operations WHERE run_id=?", (rid,))
    assert {record["id"] for record in records} == set(operations.values())
    assert all(record["status"] == "succeeded" for record in records)


def test_unknown_runtime_embedding_requires_api_consent_and_keeps_pending_identity(
    client, model, embedding_model, monkeypatch
):
    embedding_calls = []

    async def embed(model, texts, secret):
        if texts == ["question"]:
            embedding_calls.append(texts)
            if len(embedding_calls) == 1:
                raise OSError("connection lost after sending")
        return await vectors(model, texts, secret)

    monkeypatch.setattr(knowledge, "embeddings", embed)
    monkeypatch.setattr(provider, "chat", search_once)
    library = create_kb(client, embedding_model)
    assert upload(client, library)["status"] == "ready"
    rid = start(client, model, [library])
    state = wait_run(client, rid)
    assert state["status"] == "failed" and state["resumable"]
    assert state["requires_model_retry"] is True
    original = checkpoint(rid)
    operation_id = original["frames"]["main"]["pending"]["handler_state"]["knowledge_search"][
        "operations"
    ][library["id"]]
    refused = client.post(f"/api/v1/runs/{rid}/resume", json={"input": "do not accept yet"})
    assert refused.status_code == 409 and "retry_unknown_models" in refused.json()["detail"]
    assert checkpoint(rid) == original and len(embedding_calls) == 1
    assert client.get(f"/api/v1/runs/{rid}").json()["status"] == "failed"
    resumed = client.post(
        f"/api/v1/runs/{rid}/resume", json={"retry_unknown_models": True, "stream": True}
    )
    assert resumed.status_code == 200 and "run.completed" in resumed.text
    assert wait_run(client, rid)["status"] == "succeeded"
    assert len(embedding_calls) == 2
    assert db.query("SELECT id FROM embedding_operations WHERE run_id=?", (rid,)) == [
        {"id": operation_id}
    ]


def test_same_query_in_separate_tool_calls_has_separate_embedding_operations(
    client, model, embedding_model, monkeypatch
):
    seen = []

    async def embed(model, texts, secret):
        if texts == ["question"]:
            seen.append(texts)
        return await vectors(model, texts, secret)

    async def twice(model, messages, tools, secret):
        if not tools:
            return decision()
        completed = sum(message["role"] == "tool" for message in messages)
        if completed >= 2:
            return answer("two independent searches")
        return call("knowledge_search", {"query": "question"}, cid=f"query-{completed + 1}")

    monkeypatch.setattr(knowledge, "embeddings", embed)
    monkeypatch.setattr(provider, "chat", twice)
    library = create_kb(client, embedding_model)
    assert upload(client, library)["status"] == "ready"
    rid = start(client, model, [library])
    assert wait_run(client, rid)["status"] == "succeeded"
    records = db.query("SELECT id FROM embedding_operations WHERE run_id=?", (rid,))
    assert len(seen) == len(records) == len({row["id"] for row in records}) == 2


def test_cancellation_after_embedding_before_tool_completion_reuses_paid_result(
    client, embedding_model, monkeypatch, tmp_path
):
    query_calls = []

    async def embed(model, texts, secret):
        if texts == ["question"]:
            query_calls.append(texts)
        return await vectors(model, texts, secret)

    monkeypatch.setattr(knowledge, "embeddings", embed)
    monkeypatch.setattr(provider, "chat", search_once)
    library = create_kb(client, embedding_model)
    assert upload(client, library)["status"] == "ready"
    space = db.query("SELECT id FROM spaces", one=True)["id"]
    actor = db.query("SELECT id FROM users", one=True)["id"]
    snap = snapshot()
    snap["config"]["wiki"] = [library["id"]]
    snap["wiki"] = [db.resource(library["id"], space, "wiki")]
    saves = []

    async def scenario():
        got_vectors = asyncio.Event()

        async def stopped_after_search(*args, **kwargs):
            await knowledge.search(*args, **kwargs)
            got_vectors.set()
            await asyncio.Event().wait()

        adapter = RunKnowledgeSearch(
            space, actor_id=actor, run_id=None, search=stopped_after_search
        )
        engine = create_engine(
            snap,
            tmp_path,
            lambda *args: None,
            lambda value: value,
            adapter,
            save=lambda state: saves.append(copy.deepcopy(state)),
        )
        task = asyncio.create_task(engine.execute("search"))
        await got_vectors.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await engine.close()
        saved = saves[-1]
        original_ids = saved["frames"]["main"]["pending"]["handler_state"]["knowledge_search"][
            "operations"
        ]
        assert len(query_calls) == 1
        adapter = RunKnowledgeSearch(space, actor_id=actor, run_id=None)
        resumed = create_engine(
            snap,
            tmp_path,
            lambda *args: None,
            lambda value: value,
            adapter,
            checkpoint=copy.deepcopy(saved),
        )
        try:
            resumed.resume()
            assert await resumed.execute("search") == "found evidence"
        finally:
            await resumed.close()
        rows = db.query("SELECT id FROM embedding_operations WHERE purpose='query'")
        assert {row["id"] for row in rows} == set(original_ids.values())
        assert len(query_calls) == 1

    asyncio.run(scenario())


def test_host_deadline_preserves_pending_in_actual_tool_runtime(tmp_path, monkeypatch):
    async def wait_forever(*args, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(provider, "chat", search_once)
    snap = snapshot()
    snap["config"]["wiki"] = ["wiki"]
    snap["wiki"] = [{"id": "wiki", "embedding_id": "model"}]
    adapter = RunKnowledgeSearch(
        "space", actor_id="actor", run_id=None, search=wait_forever, timeout=0.01
    )
    engine = create_engine(snap, tmp_path, lambda *args: None, lambda value: value, adapter)

    async def scenario():
        try:
            with pytest.raises(HookRecoveryRequired, match="执行时限"):
                await engine.execute("search")
            pending = engine.state["frames"]["main"]["pending"]
            assert pending["index"] == 0 and pending["stage"] == "executing"
            assert pending["handler_state"]["knowledge_search"]["operations"]["wiki"]
            operation = pending["handler_state"]["__tool_runtime_operation"]
            assert operation["actual_status"] == "unknown" and "raw_output" not in operation
            binding = next(
                row for row in engine.state["modules"]["tools"] if row["name"] == "knowledge_search"
            )
            assert binding["timeout"] is None and binding["resume_inflight"] is True
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_legacy_checkpoint_keeps_original_tool_binding_and_nonreplay_policy(tmp_path):
    async def legacy(*args):
        return []

    snap = snapshot()
    previous = create_engine(snap, tmp_path, lambda *args: None, lambda value: value, legacy)
    saved = copy.deepcopy(previous.state)
    asyncio.run(previous.close())
    adapter = run_knowledge_search("space", actor_id="actor", run_id="run", checkpoint=saved)
    assert not hasattr(adapter, "search_with_context")
    restored = create_engine(
        snap, tmp_path, lambda *args: None, lambda value: value, adapter, checkpoint=saved
    )
    assert restored.state["modules"] == saved["modules"]
    binding = next(row for row in saved["modules"]["tools"] if row["name"] == "knowledge_search")
    assert binding["timeout"] == 90 and binding["resume_inflight"] is False
    asyncio.run(restored.close())


def test_invocation_identity_must_persist_before_any_search_dispatch():
    dispatched = []

    class BrokenInvocation:
        def get(self, key):
            return None

        def set(self, key, value):
            raise ToolPersistenceError("checkpoint unavailable")

    async def search(*args, **kwargs):
        dispatched.append(True)

    adapter = RunKnowledgeSearch("space", actor_id="actor", run_id=None, search=search)
    with pytest.raises(ToolPersistenceError):
        asyncio.run(
            adapter.search_with_context(
                [{"id": "wiki", "embedding_id": "model"}], "q", BrokenInvocation()
            )
        )
    assert dispatched == []
