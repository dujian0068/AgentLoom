"""Cross-run journal, bounded projections and original-run recovery boundaries."""

import json
import time
from copy import deepcopy

import pytest
from agentloom import store as db
from agentloom.security import decrypt, encrypt
from agentloom.services import session_history as history
from agentloom_runtime import provider
from agentloom_runtime.context_validation import validate_compaction
from test_harness import answer, call, decision
from test_platform import add_agent, wait_run


def publish(client, model, **kwargs):
    agent = add_agent(client, model, **kwargs)
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    return agent["id"]


def run(client, aid, text, sid=None):
    response = client.post(f"/api/v1/agents/{aid}/runs", json={"input": text, "session_id": sid})
    assert response.status_code == 200, response.text
    value = response.json()
    return value, wait_run(client, value["run_id"])


def checkpoint(rid):
    return json.loads(
        decrypt(db.query("SELECT payload FROM checkpoints WHERE run_id=?", (rid,), True)["payload"])
    )


def test_eight_questions_inherit_tool_records_once_and_encrypt_journal(client, model, monkeypatch):
    seen, current = [], {"question": ""}

    async def chat(m, messages, tools, secret):
        if not tools:
            return decision()
        seen.append(deepcopy(messages))
        if current["question"] == "Q0" and not any(x["role"] == "tool" for x in messages):
            return call("workspace_write", {"path": "origin.txt", "content": "durable fact"})
        return answer("answer-" + current["question"])

    monkeypatch.setattr(provider, "chat", chat)
    aid, sid = publish(client, model), None
    for i in range(8):
        current["question"] = f"Q{i}"
        value, row = run(client, aid, current["question"], sid)
        sid = value["session_id"]
        assert row["status"] == "succeeded", row
    final = seen[-1]
    assert sum(x.get("content") == "Q0" for x in final) == 1
    assert any(x["role"] == "tool" and "origin.txt" in x["content"] for x in final)
    context = checkpoint(value["run_id"])["frames"]["main"]["context"]
    assert context["user_turns"] == 8 and context["model_steps"] == 9
    raw = db.query("SELECT payload FROM session_messages WHERE session_id=?", (sid,))
    assert all("durable fact" not in item["payload"] for item in raw)
    records = client.get(f"/api/v1/sessions/{sid}/messages", params={"limit": 200}).json()["items"]
    assert sum(item["kind"] == "user_input" for item in records) == 8
    assert any("durable fact" in json.dumps(item) for item in records)


def test_failed_work_and_supplement_survive_but_old_resume_does_not_see_later_question(
    client, model, monkeypatch
):
    mode, captured = {"value": "fail"}, []

    async def chat(m, messages, tools, secret):
        if not tools:
            return decision()
        captured.append(deepcopy(messages))
        if mode["value"] == "fail":
            if not any(x["role"] == "tool" for x in messages):
                return call(
                    "workspace_write", {"path": "saved.txt", "content": "partial valid result"}
                )
            raise RuntimeError("disconnect")
        return answer("done")

    monkeypatch.setattr(provider, "chat", chat)
    aid = publish(client, model)
    first, row = run(client, aid, "original-A")
    assert row["status"] == "failed"
    mode["value"] = "ok"
    _, row = run(client, aid, "later-B", first["session_id"])
    assert row["status"] == "succeeded"
    assert "partial valid result" in json.dumps(captured[-1])
    resumed = client.post(
        f"/api/v1/runs/{first['run_id']}/resume",
        json={"input": "supplement-A", "retry_unknown_models": True},
    )
    assert resumed.status_code == 200
    assert wait_run(client, first["run_id"])["status"] == "succeeded"
    assert "later-B" not in json.dumps(captured[-1])
    assert "supplement-A" in json.dumps(captured[-1])
    _, row = run(client, aid, "new-C", first["session_id"])
    assert row["status"] == "succeeded"
    assert "later-B" in json.dumps(captured[-1]) and "supplement-A" in json.dumps(captured[-1])
    assert sum(m.get("content") == "original-A" for m in captured[-1]) == 1


def test_history_projection_closes_unknown_calls_and_keeps_late_results():
    messages = [
        call("workspace_write", {"path": "x", "content": "x"}),
        {"role": "user", "content": "new question"},
        {"role": "tool", "tool_call_id": "call-1", "content": "late result"},
    ]
    original = deepcopy(messages)
    result = history.project(messages)
    system = {"role": "system", "content": "rules"}
    validate_compaction([system], [system, *result])
    assert messages == original
    assert "unknown" in result[1]["content"]
    assert result[-1]["role"] == "user" and "late result" in result[-1]["content"]


def test_same_call_id_in_different_runs_never_claims_the_other_result(client):
    sid = "scope-regression"
    with db.transaction("session:" + sid) as c:
        history.append(
            c,
            sid,
            "A",
            "main",
            "1",
            "model",
            call("workspace_write", {"path": "a", "content": "a"}),
        )
        history.append(
            c, sid, "B", "main", "1", "user_input", {"role": "user", "content": "task-B"}
        )
        history.append(
            c,
            sid,
            "B",
            "main",
            "2",
            "model",
            call("workspace_write", {"path": "b", "content": "b"}),
        )
        history.append(
            c,
            sid,
            "A",
            "main",
            "2",
            "tool",
            {"role": "tool", "tool_call_id": "call-1", "content": "result-only-A"},
        )
        messages, _ = history.inherit(c, sid)
    results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
    assert len(results) == 2 and all(value["status"] == "unknown" for value in results)
    assert any(m["role"] == "user" and "result-only-A" in m["content"] for m in messages)


def test_compaction_snapshot_and_counters_pass_to_next_question(client, model, monkeypatch):
    seen = []

    async def chat(m, messages, tools, secret):
        if "CONTEXT_COMPACTION" in messages[0]["content"]:
            return answer("已确认第一轮资料，标识ORIGIN，继续当前任务。")
        if not tools:
            return decision()
        seen.append(deepcopy(messages))
        return answer("verified answer")

    monkeypatch.setattr(provider, "chat", chat)
    aid = publish(client, model, context_policy={"user_turns": 2})
    a, row = run(client, aid, "ORIGIN " + "history-data " * 350)
    assert row["status"] == "succeeded"
    b, row = run(client, aid, "second", a["session_id"])
    assert row["status"] == "succeeded", row
    assert any(e["kind"] == "context.compacted" for e in row["events"])
    bctx = checkpoint(b["run_id"])["frames"]["main"]["context"]
    assert bctx["baseline_user_turns"] == 2
    c, row = run(client, aid, "third", a["session_id"])
    assert row["status"] == "succeeded", row
    cctx = checkpoint(c["run_id"])["frames"]["main"]["context"]
    assert cctx["user_turns"] == 3 and cctx["baseline_user_turns"] == 2
    assert cctx["model_steps"] == 3
    assert "ORIGIN" in json.dumps(seen[-1]) and "history-data " * 10 not in json.dumps(seen[-1])
    original = client.get(f"/api/v1/sessions/{a['session_id']}/messages").json()["items"]
    assert "history-data " * 10 in json.dumps(original)


def test_checkpoint_and_journal_rollback_together(client, model, monkeypatch):
    async def chat(*args):
        return decision() if not args[2] else answer("done")

    monkeypatch.setattr(provider, "chat", chat)
    value, _ = run(client, publish(client, model), "atomic")
    rid = value["run_id"]
    state = checkpoint(rid)
    previous = deepcopy(state)
    records = state["frames"]["main"]["context"]["records"]
    records.append(
        {
            "seq": records[-1]["seq"] + 1,
            "kind": "feedback",
            "message": {"role": "user", "content": "rollback-me"},
        }
    )

    def fail(*args):
        raise RuntimeError("snapshot failure")

    monkeypatch.setattr(history, "save_view", fail)
    with pytest.raises(RuntimeError, match="snapshot failure"):
        history.save_checkpoint(rid, state)
    assert checkpoint(rid) == previous
    items = client.get(f"/api/v1/sessions/{value['session_id']}/messages").json()["items"]
    assert "rollback-me" not in json.dumps(items)


def test_nonprefix_resumed_snapshot_cannot_replace_newer_history(client, model, monkeypatch):
    async def chat(*args):
        return decision() if not args[2] else answer("done")

    monkeypatch.setattr(provider, "chat", chat)
    aid = publish(client, model)
    a, _ = run(client, aid, "A")
    b, _ = run(client, aid, "B", a["session_id"])
    sid = a["session_id"]
    state = checkpoint(a["run_id"])
    frame = state["frames"]["main"]
    frame["context"]["compactions"] = [{"revision": 1}]
    frame["context"]["records"].append(
        {"seq": 3, "kind": "model", "message": {"role": "assistant", "content": "late-A"}}
    )
    history.save_checkpoint(a["run_id"], state)
    assert db.query("SELECT * FROM session_views WHERE run_id=?", (a["run_id"],)) == []
    with db.transaction("session:" + sid) as c:
        messages, meta = history.inherit(c, sid)
    assert "B" in json.dumps(messages) and "late-A" in json.dumps(messages)
    assert meta["user_turns"] == 2 and meta["model_steps"] == 3
    assert b["run_id"] != a["run_id"]


def test_history_pagination_and_member_isolation(client, model, monkeypatch):
    from agentloom.security import digest

    async def chat(*args):
        return decision() if not args[2] else answer("private")

    monkeypatch.setattr(provider, "chat", chat)
    value, _ = run(client, publish(client, model), "private input")
    url = f"/api/v1/sessions/{value['session_id']}/messages"
    first = client.get(url, params={"limit": 1}).json()
    second = client.get(url, params={"after": first["next_after"], "limit": 1}).json()
    assert first["items"][0]["seq"] < second["items"][0]["seq"]
    me = client.get("/api/me").json()
    uid = db.uid()
    db.execute("INSERT INTO users VALUES(?,?,?,?)", (uid, "other@history.test", "other", "x"))
    db.execute("INSERT INTO members VALUES(?,?,?)", (uid, me["space"]["id"], "owner"))
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest("history-other"), uid, me["space"]["id"], "api", "other", time.time() + 1000),
    )
    assert client.get(url, headers={"Authorization": "Bearer history-other"}).status_code == 404


def test_legacy_adoption_is_idempotent_and_can_resume_with_matching_local_keys(
    client, model, monkeypatch
):
    async def chat(*args):
        return decision() if not args[2] else answer("old answer")

    monkeypatch.setattr(provider, "chat", chat)
    value, _ = run(client, publish(client, model), "old question")
    state = checkpoint(value["run_id"])
    state["frames"]["main"].pop("context")
    db.execute("DELETE FROM session_messages WHERE session_id=?", (value["session_id"],))
    db.execute(
        "UPDATE checkpoints SET payload=? WHERE run_id=?",
        (encrypt(json.dumps(state)), value["run_id"]),
    )
    with db.transaction("session:" + value["session_id"]) as c:
        history.inherit(c, value["session_id"])
    state = checkpoint(value["run_id"])
    history.save_checkpoint(value["run_id"], state)
    rows = client.get(f"/api/v1/sessions/{value['session_id']}/messages").json()["items"]
    assert sum(x["kind"] == "user_input" for x in rows) == 1
    assert state["frames"]["main"]["context"]["raw_complete"] is False
