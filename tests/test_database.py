"""Storage behavior verified on SQLite and optional isolated PostgreSQL schemas."""

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from agentloom import database, knowledge
from agentloom import store as db
from test_platform import add_agent


def test_database_parameters_and_rollback(client):
    # Bind values with SQL metacharacters; literal question/percent signs stay literals.
    value = "中文 ' OR 1=1 -- ? 50%"
    row = db.query("SELECT ? AS value, '?' AS question, '50%' AS percent", (value,), True)
    assert row == {"value": value, "question": "?", "percent": "50%"}
    with pytest.raises(RuntimeError, match="rollback"):
        with db.transaction("rollback-test") as c:
            c.execute("INSERT INTO spaces VALUES(?,?,?)", ("rolled-back", "test", "invite"))
            raise RuntimeError("rollback")
    assert db.query("SELECT id FROM spaces WHERE id=?", ("rolled-back",)) == []


def test_timestamps_and_upsert_preserve_values(client):
    timestamp = time.time() + 0.123456
    db.execute("INSERT INTO checkpoints VALUES(?,?,?)", ("checkpoint", "first", timestamp))
    row = db.query("SELECT updated FROM checkpoints WHERE run_id=?", ("checkpoint",), True)
    assert abs(row["updated"] - timestamp) < 0.000001
    resource = db.put_resource("space", "skills", {"name": "before"})
    db.put_resource("space", "skills", {"name": "after"}, resource["id"])
    assert db.resource(resource["id"], "space")["name"] == "after"
    assert len(db.list_resources("space", "skills")) == 1


def test_concurrent_publish_allocates_distinct_versions(client, model):
    agent = add_agent(client, model)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(lambda _: client.post(f"/api/agents/{agent['id']}/publish"), range(8))
        )
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["version"] for response in responses) == list(range(1, 9))


def test_concurrent_document_upload_increments_revision(client):
    kb = client.post("/api/wiki", json={"name": "concurrent"}).json()
    space = client.get("/api/me").json()["space"]["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(
            pool.map(
                lambda i: asyncio.run(
                    knowledge.add_document(space, kb, f"{i}.md", "订单架构 customer_orders")
                ),
                range(2),
            )
        )
    revisions = db.query(
        "SELECT revision FROM documents WHERE kb_id=? ORDER BY revision", (kb["id"],)
    )
    assert [row["revision"] for row in revisions] == [1, 2]
    assert db.resource(kb["id"], space)["revision"] == 2
    assert len(ids) == 2


def test_missing_postgres_password_does_not_fall_back(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "postgresql")
    monkeypatch.setenv("DB_PASSWORD", "")
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        database.postgres_settings()
    assert database.backend() == "postgresql"


def test_database_outage_returns_safe_error(client, monkeypatch):
    def broken(*args, **kwargs):
        raise psycopg.OperationalError("do-not-expose-database-credentials")

    monkeypatch.setattr(db, "query", broken)
    response = client.get("/api/health")
    assert response.status_code == 503
    assert "do-not-expose" not in response.text
    assert "数据库" in response.json()["detail"]


def test_disconnected_finalization_recovers_without_replaying_task(client, monkeypatch):
    from agentloom.services import runs
    from agentloom.state import PENDING_FINALIZATIONS

    rid = db.uid()
    db.execute("INSERT INTO runs(id,status) VALUES(?,?)", (rid, "running"))
    original = db.finalize

    def disconnected(*args, **kwargs):
        raise psycopg.OperationalError("offline")

    monkeypatch.setattr(db, "finalize", disconnected)
    assert not runs.finish_run(rid, "failed", "run.failed", {"error": "database unavailable"})
    assert rid in PENDING_FINALIZATIONS
    monkeypatch.setattr(db, "finalize", original)
    assert runs.flush_finalizations(rid) == 1
    assert rid not in PENDING_FINALIZATIONS
    assert db.query("SELECT status FROM runs WHERE id=?", (rid,), True)["status"] == "failed"
    assert len(db.query("SELECT seq FROM events WHERE run_id=?", (rid,))) == 1


def test_unknown_commit_finalization_does_not_duplicate_end_event(client, monkeypatch):
    from agentloom.services import runs

    rid = db.uid()
    db.execute("INSERT INTO runs(id,status) VALUES(?,?)", (rid, "running"))
    original = db.finalize

    def committed_but_disconnected(*args, **kwargs):
        original(*args, **kwargs)
        raise psycopg.OperationalError("commit acknowledgement lost")

    monkeypatch.setattr(db, "finalize", committed_but_disconnected)
    assert not runs.finish_run(rid, "succeeded", "run.completed", {"output": "done"}, output="done")
    monkeypatch.setattr(db, "finalize", original)
    runs.flush_finalizations(rid)
    row = db.query("SELECT status,output FROM runs WHERE id=?", (rid,), True)
    assert row == {"status": "succeeded", "output": "done"}
    assert len(db.query("SELECT seq FROM events WHERE run_id=?", (rid,))) == 1


def test_startup_failure_closes_database_pool(monkeypatch):
    from agentloom.app import app
    from fastapi.testclient import TestClient

    closed = []

    def broken():
        raise RuntimeError("startup failed")

    monkeypatch.setattr(db, "init", broken)
    monkeypatch.setattr(db, "close", lambda: closed.append(True))
    with pytest.raises(RuntimeError, match="startup failed"):
        with TestClient(app):
            pass
    assert closed == [True]
