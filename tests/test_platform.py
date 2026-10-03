import asyncio
import json
import time
from pathlib import Path
from unittest.mock import patch

from agentloom import store as db
from agentloom.app import app
from agentloom_runtime import engine
from agentloom_runtime.sandbox import command
from fastapi.testclient import TestClient


def config(model, **kwargs):
    return {
        "name": "测试 Agent",
        "model": model["id"],
        "prompt": "回答问题",
        "mode": "react",
        "skills": [],
        "tools": [],
        "wiki": [],
        "subs": [],
        **kwargs,
    }


def add_agent(client, model, **kwargs):
    r = client.post("/api/agents", json=config(model, **kwargs))
    assert r.status_code == 200
    return r.json()


def wait_run(client, rid):
    for _ in range(100):
        row = client.get("/api/v1/runs/" + rid).json()
        if row["status"] not in ("queued", "running"):
            return row
        time.sleep(0.02)
    raise AssertionError("run timeout")


def test_publish_required_and_immutable(client, model):
    a = add_agent(client, model)
    assert client.post(f"/api/v1/agents/{a['id']}/runs", json={"input": "hi"}).status_code == 400
    assert client.post(f"/api/agents/{a['id']}/publish").json()["version"] == 1
    new = config(model, prompt="新的草稿", name="改名")
    assert client.put("/api/agents/" + a["id"], json=new).status_code == 200
    snap = client.get(f"/api/agents/{a['id']}/versions/1").json()
    assert snap["config"]["prompt"] == "回答问题" and snap["config"]["name"] == "测试 Agent"
    assert "secret" not in snap["model_obj"]
    assert client.delete("/api/resources/" + model["id"]).status_code == 400


def test_space_api_key_and_secret_protection(client, model):
    response = client.post("/api/api-keys", json={"name": "test"})
    key = response.json()["key"]
    with TestClient(app) as stranger:
        assert stranger.get("/api/agents").status_code == 401
        assert stranger.get("/api/resources/models").status_code == 401
        assert (
            stranger.get("/api/agents", headers={"Authorization": "Bearer " + key}).status_code
            == 200
        )
    space = db.uid()
    uid = db.uid()
    db.execute("INSERT INTO users VALUES(?,?,?,?)", (uid, "stranger@test", "其他人", "x"))
    db.execute("INSERT INTO members VALUES(?,?,?)", (uid, space, "owner"))
    from agentloom.security import digest

    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest("other"), uid, space, "api", "other", time.time() + 100),
    )
    assert (
        client.post(
            "/api/models/" + model["id"] + "/test", headers={"Authorization": "Bearer other"}
        ).status_code
        == 400
    )
    records = client.get("/api/api-keys").json()
    client.delete("/api/api-keys/" + records[0]["id"])
    assert client.get("/api/agents", headers={"Authorization": "Bearer " + key}).status_code == 401
    assert "unit-test-secret" not in client.get("/api/resources/models").text
    assert client.post("/api/models", json={"api_key": "bad-secret"}).status_code == 422
    assert "bad-secret" not in client.post("/api/models", json={"api_key": "bad-secret"}).text


def test_skill_upload_paths_and_metadata(client):
    body = b"---\nname: example\ndescription: example skill\n---\nRead references/info.txt."
    r = client.post(
        "/api/skills/upload",
        files=[
            ("files", ("example/SKILL.md", body)),
            ("files", ("example/references/info.txt", b"hello")),
        ],
    )
    assert r.status_code == 200
    skill = r.json()
    assert skill["name"] == "example" and "path" not in skill
    assert (
        client.get(
            "/api/skills/" + skill["id"] + "/file", params={"path": "references/info.txt"}
        ).content
        == b"hello"
    )
    assert (
        client.get(
            "/api/skills/" + skill["id"] + "/file", params={"path": "../../encryption.key"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/skills/upload", files={"files": ("../escape.txt", b"bad")}).status_code
        == 400
    )


def test_rag_upload_search_delete_and_scope(client):
    kb = client.post("/api/wiki", json={"name": "研发资料"}).json()
    r = client.post(
        "/api/wiki/" + kb["id"] + "/upload",
        files=[
            (
                "files",
                ("schema.sql", b"CREATE TABLE customer_orders (order_id INT, customer_id INT);"),
            ),
            ("files", ("intro.md", "订单架构采用事件驱动。".encode())),
        ],
    )
    assert all(x["status"] == "ready" for x in r.json())
    rows = client.post(
        "/api/wiki/" + kb["id"] + "/search", json={"query": "customer_orders"}
    ).json()
    assert rows[0]["file"] == "schema.sql"
    assert client.post("/api/wiki/" + kb["id"] + "/search", json={"query": "架构"}).json()
    assert (
        client.post("/api/wiki/" + kb["id"] + "/search", json={"query": "totally_unknown"}).json()
        == []
    )
    doc = rows[0]["document_id"]
    client.delete("/api/documents/" + doc)
    assert not client.post(
        "/api/wiki/" + kb["id"] + "/search", json={"query": "customer_orders"}
    ).json()
    assert client.get("/api/documents/" + doc).status_code == 404


def test_subagent_no_model_or_mode(client, model):
    child = {
        "id": "child",
        "name": "助手",
        "description": "执行子任务",
        "prompt": "",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    assert (
        client.post("/api/agents", json=config(model, subs=[{**child, "mode": "plan"}])).status_code
        == 422
    )
    assert (
        client.post(
            "/api/agents", json=config(model, subs=[{**child, "model": model["id"]}])
        ).status_code
        == 422
    )
    assert client.post("/api/agents", json=config(model, subs=[child])).status_code == 200


def test_actual_engine_delegation_and_model_inheritance(client, model, monkeypatch):
    child = {
        "id": "child",
        "name": "子任务助手",
        "description": "处理资料",
        "prompt": "child prompt",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    seen = []

    async def fake_chat(m, messages, tools, secret):
        seen.append(m["model_id"])
        if "TASK_COMPLETION_REVIEW" in messages[0]["content"]:
            return {
                "role": "assistant",
                "content": json.dumps(
                    {"decision": "complete", "reason": "目标已满足", "next_action": ""}
                ),
            }
        if "child prompt" in messages[0]["content"]:
            return {"role": "assistant", "content": "子任务结果"}
        if any(x.get("role") == "tool" for x in messages):
            return {"role": "assistant", "content": "汇总子任务结果"}
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "delegate_task",
                        "arguments": json.dumps({"subagent_id": "child", "task": "分析资料"}),
                    },
                }
            ],
            "content": None,
        }

    monkeypatch.setattr(engine, "chat", fake_chat)
    a = add_agent(client, model, subs=[child])
    client.post("/api/agents/" + a["id"] + "/publish")
    result = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "复杂任务"}).json()
    row = wait_run(client, result["run_id"])
    assert row["status"] == "succeeded" and row["output"] == "汇总子任务结果"
    assert set(seen) == {"test-model"}
    assert any(e["kind"] == "subagent.created" for e in row["events"])
    assert any(e["kind"] == "subagent.completed" for e in row["events"])


def test_plan_step_execution_and_artifacts(client, model, monkeypatch):
    action_calls = 0

    async def fake_chat(m, messages, tools, secret):
        nonlocal action_calls
        if "TASK_COMPLETION_REVIEW" in messages[0]["content"]:
            return {
                "role": "assistant",
                "content": json.dumps(
                    {"decision": "complete", "reason": "报告文件已读取验证", "next_action": ""}
                ),
            }
        actions = [
            (
                "update_plan",
                {
                    "steps": [{"id": "report", "step": "生成并验证报告", "status": "in_progress"}],
                    "explanation": "开始任务",
                },
            ),
            ("workspace_write", {"path": "report.md", "content": "# Report"}),
            ("workspace_read", {"path": "report.md"}),
            (
                "update_plan",
                {
                    "steps": [{"id": "report", "step": "生成并验证报告", "status": "completed"}],
                    "explanation": "已读回文件验证",
                },
            ),
        ]
        if action_calls >= len(actions):
            return {"role": "assistant", "content": "报告已生成"}
        name, args = actions[action_calls]
        action_calls += 1
        return {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call-" + str(action_calls),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
            "content": None,
        }

    monkeypatch.setattr(engine, "chat", fake_chat)
    a = add_agent(client, model, mode="plan")
    client.post("/api/agents/" + a["id"] + "/publish")
    r = client.post(
        "/api/v1/agents/" + a["id"] + "/runs", json={"input": "生成报告", "stream": True}
    )
    assert r.status_code == 200 and "plan.created" in r.text
    rid = r.headers["x-run-id"]
    row = client.get("/api/v1/runs/" + rid).json()
    assert row["status"] == "succeeded" and "report.md" in row["artifacts"]
    assert (
        client.get("/api/v1/runs/" + rid + "/artifact", params={"path": "report.md"}).content
        == b"# Report"
    )
    assert (
        client.get(
            "/api/v1/runs/" + rid + "/artifact", params={"path": "../../encryption.key"}
        ).status_code
        == 400
    )


def test_cancel_run(client, model, monkeypatch):
    async def slow(*args):
        await asyncio.sleep(60)

    monkeypatch.setattr(engine, "chat", slow)
    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")
    r = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "test"}).json()
    time.sleep(0.03)
    client.post("/api/v1/runs/" + r["run_id"] + "/cancel")
    assert wait_run(client, r["run_id"])["status"] == "cancelled"


def test_missing_docker_never_executes_host_scripts(tmp_path):
    with patch("agentloom_runtime.sandbox.shutil.which", return_value=None):
        try:
            asyncio.run(command(tmp_path, [], "touch bad"))
        except RuntimeError:
            pass
        else:
            raise AssertionError("must reject")
    assert not (tmp_path / "bad").exists()


def test_real_mcp_http_discovery_and_call(client):
    import socket
    import subprocess
    import sys

    import httpx

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    p = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("mcp_server.py")), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            try:
                httpx.get(f"http://127.0.0.1:{port}/mcp", timeout=0.1)
                break
            except httpx.RequestError:
                time.sleep(0.03)
        r = client.post(
            "/api/tools/external",
            json={
                "name": "本地测试工具",
                "endpoint": f"http://127.0.0.1:{port}/mcp",
                "transport": "streamable-http",
            },
        )
        assert r.status_code == 200, r.text
        tool = r.json()
        assert tool["status"] == "ready", tool
        assert tool["schemas"][0]["name"] == "add"
        result = client.post(
            "/api/tools/" + tool["id"] + "/test",
            json={"name": "add", "arguments": {"a": 3, "b": 5}},
        )
        assert result.status_code == 200 and not result.json()["is_error"]
        assert result.json()["content"][0]["text"] == "8"
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_hybrid_rag_with_embedding_connection(client, monkeypatch):
    from agentloom import knowledge

    async def embed(model, texts, secret):
        return [[1.0, 0.0] if "architecture" in t or "架构" in t else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(knowledge, "embeddings", embed)
    em = client.post(
        "/api/models",
        json={
            "name": "Embedding",
            "provider": "openai",
            "base_url": "https://example.test/v1",
            "model_id": "embed-test",
            "api_key": "test-embedding",
            "purpose": "embedding",
        },
    ).json()
    kb = client.post("/api/wiki", json={"name": "混合索引", "embedding_id": em["id"]}).json()
    r = client.post(
        "/api/wiki/" + kb["id"] + "/upload",
        files=[
            ("files", ("architecture.md", b"architecture uses events")),
            ("files", ("other.md", b"apples and bananas")),
        ],
    )
    assert all(x["status"] == "ready" for x in r.json())
    found = client.post("/api/wiki/" + kb["id"] + "/search", json={"query": "架构"}).json()
    assert found[0]["file"] == "architecture.md"
    assert client.get("/api/resources/wiki").json()[0]["retrieval"] == "hybrid"
