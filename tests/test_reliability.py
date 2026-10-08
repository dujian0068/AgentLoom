"""Failure boundaries, concurrent event writes and legacy migration coverage."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from agentloom import store as db
from agentloom.state import TASKS
from agentloom_runtime import provider
from test_platform import add_agent, wait_run

MODEL = {"model_id": "test-model", "base_url": "https://model.example.test/v1"}
SECRET = "never-expose-this-key"


def transport(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        provider.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_transient_provider_error_retries_and_returns_valid_message(monkeypatch):
    requests = []
    delays = []

    def handler(request):
        requests.append(request)
        if len(requests) < 3:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    async def sleep(delay):
        delays.append(delay)

    transport(monkeypatch, handler)
    monkeypatch.setattr(provider.asyncio, "sleep", sleep)
    assert asyncio.run(provider.chat(MODEL, [], [], SECRET))["content"] == "ok"
    assert len(requests) == 3 and delays == [0, 0]


@pytest.mark.parametrize(
    "vendor,parameter", [("openai", "max_completion_tokens"), ("deepseek", "max_tokens")]
)
def test_reserved_output_budget_is_sent_to_provider(monkeypatch, vendor, parameter):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
        )

    transport(monkeypatch, handler)
    asyncio.run(
        provider.chat({**MODEL, "provider": vendor, "max_output_tokens": 2048}, [], [], SECRET)
    )
    assert requests[0][parameter] == 2048
    assert ("max_tokens" if vendor == "openai" else "max_completion_tokens") not in requests[0]


def test_credentials_error_not_retried_or_exposed(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(401, json={"message": SECRET})

    transport(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(provider.chat(MODEL, [], [], SECRET))
    assert len(requests) == 1 and SECRET not in str(exc.value) and "401" in str(exc.value)


def test_read_timeout_not_replayed(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        raise httpx.ReadTimeout("url with " + SECRET, request=request)

    transport(monkeypatch, handler)
    with pytest.raises(RuntimeError) as exc:
        asyncio.run(provider.chat(MODEL, [], [], SECRET))
    assert len(requests) == 1 and SECRET not in str(exc.value) and "超时" in str(exc.value)


@pytest.mark.parametrize(
    "message",
    [
        {"content": None},
        {"content": 17},
        {"tool_calls": [{"id": "x", "function": {"name": "read", "arguments": {}}}]},
    ],
)
def test_invalid_model_response_is_a_clear_error(monkeypatch, message):
    transport(
        monkeypatch, lambda req: httpx.Response(200, json={"choices": [{"message": message}]})
    )
    with pytest.raises(RuntimeError, match="有效回答或工具调用"):
        asyncio.run(provider.chat(MODEL, [], [], SECRET))


@pytest.mark.parametrize(
    "records",
    [
        [{"index": 1, "embedding": [1.0, 2.0]}],
        [{"index": 0, "embedding": []}],
        [{"index": 0, "embedding": [True, 1.0]}],
    ],
)
def test_invalid_embedding_records_are_rejected(monkeypatch, records):
    transport(monkeypatch, lambda req: httpx.Response(200, json={"data": records}))
    with pytest.raises(RuntimeError, match="数量、索引或维度"):
        asyncio.run(provider.embeddings(MODEL, ["text"], SECRET))


def test_embedding_response_retains_indices_usage_and_explicit_dimensions(monkeypatch):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [2, 3]},
                    {"index": 0, "embedding": [0, 1]},
                ],
                "usage": {"total_tokens": 5},
                "id": "embedding-request-1",
                "arbitrary_metadata": SECRET,
            },
        )

    transport(monkeypatch, handler)
    response = asyncio.run(
        provider.embedding_response(MODEL, ["first", "second"], SECRET, dimensions=2)
    )
    assert requests[0] == {
        "model": "test-model",
        "input": ["first", "second"],
        "dimensions": 2,
        "encoding_format": "float",
    }
    assert response == {
        "vectors": [[2, 3], [0, 1]],
        "indices": [1, 0],
        "usage": {"total_tokens": 5},
        "provider_request_id": "embedding-request-1",
    }
    assert asyncio.run(provider.embeddings(MODEL, ["first", "second"], SECRET)) == [
        [0, 1],
        [2, 3],
    ]


def test_cancel_during_retry_is_not_swallowed(monkeypatch):
    transport(monkeypatch, lambda req: httpx.Response(429))

    async def sleep(delay):
        raise asyncio.CancelledError()

    monkeypatch.setattr(provider.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(provider.chat(MODEL, [], [], SECRET))


def test_concurrent_events_have_unique_sequential_ids(client):
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(
            pool.map(lambda i: db.event("concurrent-run", "test.event", {"index": i}), range(80))
        )
    assert sorted(sequences) == list(range(1, 81))
    assert len(db.query("SELECT seq FROM events WHERE run_id=?", ("concurrent-run",))) == 80


def test_initialization_failure_reaches_failed_and_cleans_task(client, model, monkeypatch):
    from agentloom.services import runs

    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")

    def fail(value):
        raise RuntimeError("平台密钥文件不可用")

    monkeypatch.setattr(runs, "decrypt", fail)
    rid = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "task"}).json()[
        "run_id"
    ]
    row = wait_run(client, rid)
    assert row["status"] == "failed" and rid not in TASKS
    assert row["events"][-1]["kind"] == "run.failed"


def test_event_stream_honors_reconnect_cursor(client, model, monkeypatch):
    async def chat(m, messages, tools, secret):
        return {
            "role": "assistant",
            "content": "answer"
            if tools
            else json.dumps({"decision": "complete", "reason": "done", "next_action": ""}),
        }

    monkeypatch.setattr(provider, "chat", chat)
    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")
    rid = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "task"}).json()[
        "run_id"
    ]
    row = wait_run(client, rid)
    cursor = row["events"][-2]["seq"]
    response = client.get("/api/v1/runs/" + rid + "/events", headers={"Last-Event-ID": str(cursor)})
    data = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert (
        data and all(item["seq"] > cursor for item in data) and data[-1]["kind"] == "run.completed"
    )
    assert (
        client.get(
            "/api/v1/runs/" + rid + "/events", headers={"Last-Event-ID": "invalid"}
        ).status_code
        == 400
    )

    assert client.get("/api/v1/runs/" + rid + "/events?after=-1").status_code == 400
    assert (
        client.get("/api/v1/runs/" + rid + "/events", headers={"Last-Event-ID": "-1"}).status_code
        == 400
    )


def test_legacy_database_migrates_without_data_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB", tmp_path / "legacy.db")
    sql = (Path(db.__file__).parent / "migrations/001_initial.sql").read_text()
    with sqlite3.connect(db.DB) as c:
        c.executescript(sql)
        c.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            ("legacy", "legacy@example.test", "旧账号", "password-hash"),
        )
    db.init()
    before = db.query("SELECT * FROM schema_migrations ORDER BY version")
    assert [row["version"] for row in before] == [1, 2, 3, 4]
    assert db.query("SELECT name FROM users WHERE id=?", ("legacy",), True)["name"] == "旧账号"
    db.init()
    assert db.query("SELECT * FROM schema_migrations ORDER BY version") == before
