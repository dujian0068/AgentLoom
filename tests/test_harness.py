"""Harness behavior tests use deterministic model decisions and real file/state boundaries."""

import asyncio
import copy
import json

import pytest
from agentloom import store as db
from agentloom.security import decrypt
from agentloom_runtime import provider, sandbox
from agentloom_runtime.context import CharacterCompactionPolicy
from agentloom_runtime.runtime import create_engine
from test_platform import add_agent, config, wait_run


def answer(text):
    return {"role": "assistant", "content": text}


def call(name, arguments, cid="call-1"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": cid,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def decision(value="complete", reason="实际结果满足目标", next_action=""):
    return answer(
        json.dumps(
            {"decision": value, "reason": reason, "next_action": next_action}, ensure_ascii=False
        )
    )


def snapshot(mode="react", subs=()):
    return {
        "config": {
            "prompt": "完成用户任务",
            "mode": mode,
            "skills": [],
            "tools": [],
            "wiki": [],
            "subs": list(subs),
        },
        "model_obj": {"model_id": "inherited-model", "secret": "secret"},
        "skills": [],
        "tools": [],
        "wiki": [],
    }


def runner(tmp_path, snap=None, state=None):
    events = []
    saved = []

    async def search(*args):
        return []

    e = create_engine(
        snap or snapshot(),
        tmp_path,
        lambda k, p: events.append({"kind": k, **p}),
        lambda s: s,
        search,
        checkpoint=copy.deepcopy(state),
        save=lambda s: saved.append(copy.deepcopy(s)),
    )
    return e, events, saved


def test_dynamic_plan_repairs_failure_and_revises_steps(tmp_path, monkeypatch):
    step = lambda key, text, status: {"id": key, "step": text, "status": status}
    actions = [
        call(
            "update_plan",
            {"steps": [step("read", "读取资料", "in_progress")], "explanation": "检查资料"},
        ),
        call("workspace_read", {"path": "missing.md"}),
        call(
            "update_plan",
            {
                "steps": [
                    step("read", "读取资料", "cancelled"),
                    step("write", "生成替代报告", "in_progress"),
                ],
                "explanation": "资料不存在，改为生成明确说明缺失的报告",
            },
        ),
        call("workspace_write", {"path": "report.md", "content": "资料缺失，待补充"}),
        call("workspace_read", {"path": "report.md"}),
        call(
            "update_plan",
            {
                "steps": [
                    step("read", "读取资料", "cancelled"),
                    step("write", "生成替代报告", "completed"),
                ],
                "explanation": "已读回验证",
            },
        ),
        answer("已生成资料缺失报告"),
    ]

    async def model(m, messages, tools, secret):
        if not tools:
            return decision()
        return actions.pop(0)

    monkeypatch.setattr(provider, "chat", model)
    e, events, _ = runner(tmp_path, snapshot("plan"))
    assert asyncio.run(e.execute("资料不存在时生成说明报告")) == "已生成资料缺失报告"
    kinds = [x["kind"] for x in events]
    assert (
        "tool.failed" in kinds
        and kinds.count("plan.updated") == 2
        and "completion.checked" in kinds
    )
    assert (tmp_path / "report.md").read_text() == "资料缺失，待补充"


def test_premature_answer_continues_until_file_is_verified(tmp_path, monkeypatch):
    actions = [
        answer("我会生成报告"),
        call("workspace_write", {"path": "done.txt", "content": "actual"}),
        call("workspace_read", {"path": "done.txt"}),
        answer("报告已生成并读回验证"),
    ]
    reviews = [decision("continue", "还没有生成文件", "写入文件并读回验证"), decision()]

    async def model(m, messages, tools, secret):
        return actions.pop(0) if tools else reviews.pop(0)

    monkeypatch.setattr(provider, "chat", model)
    e, events, _ = runner(tmp_path)
    result = asyncio.run(e.execute("生成报告"))
    assert result == "报告已生成并读回验证" and (tmp_path / "done.txt").exists()
    assert [x["decision"] for x in events if x["kind"] == "completion.checked"] == [
        "continue",
        "complete",
    ]


def test_pending_plan_prevents_false_success(tmp_path, monkeypatch):
    actions = [
        call(
            "update_plan",
            {
                "steps": [{"id": "a", "step": "回答问题", "status": "pending"}],
                "explanation": "开始",
            },
        ),
        answer("回答"),
        call(
            "update_plan",
            {
                "steps": [{"id": "a", "step": "回答问题", "status": "completed"}],
                "explanation": "已回答",
            },
        ),
        answer("回答"),
    ]

    async def model(m, messages, tools, secret):
        return actions.pop(0) if tools else decision()

    monkeypatch.setattr(provider, "chat", model)
    e, events, _ = runner(tmp_path, snapshot("plan"))
    asyncio.run(e.execute("回答问题"))
    assert [x["decision"] for x in events if x["kind"] == "completion.checked"] == [
        "continue",
        "complete",
    ]


def test_compaction_keeps_tool_protocol_and_original_goal(tmp_path, monkeypatch):
    e, events, _ = runner(tmp_path)
    e = create_engine(
        snapshot(),
        tmp_path,
        lambda k, p: events.append({"kind": k, **p}),
        lambda s: s,
        None,
        compaction_policy=CharacterCompactionPolicy(1500, 500),
    )
    history = [
        {"role": "user", "content": "历史事实" + ("x" * 1800)},
        {"role": "assistant", "content": "旧的输出"},
        call("workspace_read", {"path": "old.txt"}),
        {"role": "tool", "tool_call_id": "call-1", "content": "文件事实"},
    ]
    prompts = []

    async def model(m, messages, tools, secret):
        prompts.append(messages)
        if "CONTEXT_COMPACTION" in messages[0]["content"]:
            return answer("已读取 old.txt；还需要回答当前问题。")
        return answer("最终回答") if tools else decision()

    monkeypatch.setattr(provider, "chat", model)
    assert asyncio.run(e.execute("保留我的当前目标", history)) == "最终回答"
    assert any(x["kind"] == "context.compacted" for x in events)
    action = next(p for p in prompts if "CONTEXT_COMPACTION" not in p[0]["content"])
    assert "保留我的当前目标" in json.dumps(action, ensure_ascii=False)
    for i, message in enumerate(action):
        if message.get("tool_calls"):
            ids = {c["id"] for c in message["tool_calls"]}
            assert ids == {x["tool_call_id"] for x in action[i + 1 : i + 1 + len(ids)]}


def test_resume_does_not_repeat_completed_tool(tmp_path, monkeypatch):
    attempts = 0

    async def first(m, messages, tools, secret):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return call("workspace_write", {"path": "once.txt", "content": "one"})
        raise RuntimeError("provider disconnected")

    monkeypatch.setattr(provider, "chat", first)
    e, _, saved = runner(tmp_path)
    with pytest.raises(RuntimeError):
        asyncio.run(e.execute("生成文件"))
    checkpoint = saved[-1]
    assert (tmp_path / "once.txt").read_text() == "one"

    async def second(m, messages, tools, secret):
        if tools:
            assert any(x["role"] == "tool" and "once.txt" in x["content"] for x in messages)
            return answer("文件已生成")
        return decision()

    monkeypatch.setattr(provider, "chat", second)
    resumed, events, _ = runner(tmp_path, state=checkpoint)
    resumed.resume(retry_unknown_models=True)
    assert asyncio.run(resumed.execute("生成文件")) == "文件已生成"
    assert not any(x["kind"] == "tool.started" for x in events)


def test_uncertain_action_is_not_replayed(tmp_path, monkeypatch):
    snap = snapshot()
    snap["skills"] = [
        {"id": "skill", "name": "s", "description": "s", "content": "s", "path": str(tmp_path)}
    ]
    snap["config"]["skills"] = ["skill"]

    async def model(m, messages, tools, secret):
        return call("workspace_command", {"command": "write external side effect"})

    executed = []

    async def interrupted(*args):
        executed.append(True)
        raise asyncio.CancelledError()

    monkeypatch.setattr(provider, "chat", model)
    monkeypatch.setattr(sandbox, "command", interrupted)
    e, _, saved = runner(tmp_path, snap)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(e.execute("执行任务"))

    async def resume_model(m, messages, tools, secret):
        if tools:
            assert "结果未知" in messages[-1]["content"]
            return answer("需要确认外部结果")
        return decision("blocked", "外部操作结果未知", "请提供外部状态")

    monkeypatch.setattr(provider, "chat", resume_model)
    resumed, events, _ = runner(tmp_path, snap, saved[-1])
    resumed.resume()
    asyncio.run(resumed.execute("执行任务"))
    assert len(executed) == 1 and resumed.blocked
    assert any(x["kind"] == "tool.uncertain" for x in events)


def test_resume_child_preserves_same_instance_and_model(tmp_path, monkeypatch):
    child = {
        "id": "child",
        "name": "助手",
        "description": "执行任务",
        "prompt": "CHILD",
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    snap = snapshot(subs=[child])
    seen = []

    async def first(m, messages, tools, secret):
        seen.append(m["model_id"])
        if "CHILD" in messages[0]["content"]:
            raise asyncio.CancelledError()
        return call("delegate_task", {"subagent_id": "child", "task": "执行子任务"})

    monkeypatch.setattr(provider, "chat", first)
    e, _, saved = runner(tmp_path, snap)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(e.execute("复杂任务"))

    async def second(m, messages, tools, secret):
        seen.append(m["model_id"])
        if not tools:
            return decision()
        return answer("子任务完成" if "CHILD" in messages[0]["content"] else "汇总完成")

    monkeypatch.setattr(provider, "chat", second)
    resumed, events, _ = runner(tmp_path, snap, saved[-1])
    resumed.resume(retry_unknown_models=True)
    assert asyncio.run(resumed.execute("复杂任务")) == "汇总完成"
    assert resumed.state["delegations"] == 1 and set(seen) == {"inherited-model"}
    assert not any(x["kind"] == "subagent.created" for x in events)


def test_blocked_run_history_and_resume_api(client, model, monkeypatch):
    async def first(m, messages, tools, secret):
        return answer("需要项目名") if tools else decision("blocked", "缺少项目名", "提供项目名")

    monkeypatch.setattr(provider, "chat", first)
    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")
    rid = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "生成介绍"}).json()[
        "run_id"
    ]
    row = wait_run(client, rid)
    assert row["status"] == "needs_input" and row["resumable"]
    assert client.get("/api/v1/agents/" + a["id"] + "/runs").json()[0]["id"] == rid
    checkpoint = db.query("SELECT payload FROM checkpoints WHERE run_id=?", (rid,), True)["payload"]
    assert "messages" not in checkpoint and "生成介绍" not in checkpoint
    assert json.loads(decrypt(checkpoint))["frames"]["main"]["outcome"] == "blocked"
    seen = []

    async def second(m, messages, tools, secret):
        seen.append(json.dumps(messages, ensure_ascii=False))
        return answer("织点的项目介绍") if tools else decision()

    monkeypatch.setattr(provider, "chat", second)
    # A draft edit does not change the version resumed by this task.
    client.put("/api/agents/" + a["id"], json=config(model, prompt="新草稿"))
    r = client.post("/api/v1/runs/" + rid + "/resume", json={"input": "项目叫织点", "stream": True})
    assert r.status_code == 200 and "run.resumed" in r.text and "run.completed" in r.text
    row = wait_run(client, rid)
    assert (
        row["status"] == "succeeded" and row["output"] == "织点的项目介绍" and row["version"] == 1
    )
    assert any("项目叫织点" in x for x in seen)
    assert "新草稿" not in seen[0]
    assert (
        client.get("/api/v1/runs/" + rid, headers={"Authorization": "Bearer invalid"}).status_code
        == 401
    )
    assert client.post("/api/v1/runs/" + rid + "/resume", json={}).status_code == 400


def test_restart_marks_checkpoint_run_interrupted(client, model, monkeypatch):
    async def fake(m, messages, tools, secret):
        return answer("等待信息") if tools else decision("blocked", "需要输入", "补充信息")

    monkeypatch.setattr(provider, "chat", fake)
    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")
    rid = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "test"}).json()[
        "run_id"
    ]
    wait_run(client, rid)
    db.execute("UPDATE runs SET status='running' WHERE id=?", (rid,))
    db.init()
    row = client.get("/api/v1/runs/" + rid).json()
    assert row["status"] == "interrupted" and row["resumable"]
    assert (
        client.get(
            "/api/v1/runs/" + rid + "/artifact", params={"path": "checkpoint.json"}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("arguments", ["[]", "invalid-json"])
def test_malformed_tool_arguments_feed_back_without_breaking_loop(tmp_path, monkeypatch, arguments):
    first = True

    async def fake(m, messages, tools, secret):
        nonlocal first
        if not tools:
            return decision("blocked", "需要正确参数", "补充参数")
        if first:
            first = False
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "bad",
                        "type": "function",
                        "function": {"name": "workspace_write", "arguments": arguments},
                    }
                ],
            }
        assert messages[-1]["role"] == "tool" and "error" in messages[-1]["content"]
        return answer("参数有误，需要补充")

    monkeypatch.setattr(provider, "chat", fake)
    e, events, _ = runner(tmp_path)
    asyncio.run(e.execute("写文件"))
    assert e.blocked and any(x["kind"] == "tool.failed" for x in events)


def test_same_space_other_user_cannot_resume_task(client, model, monkeypatch):
    import time

    from agentloom.security import digest

    async def fake(m, messages, tools, secret):
        return answer("等待输入") if tools else decision("blocked", "需要输入", "补充信息")

    monkeypatch.setattr(provider, "chat", fake)
    a = add_agent(client, model)
    client.post("/api/agents/" + a["id"] + "/publish")
    rid = client.post("/api/v1/agents/" + a["id"] + "/runs", json={"input": "task"}).json()[
        "run_id"
    ]
    row = wait_run(client, rid)
    uid = db.uid()
    db.execute(
        "INSERT INTO users VALUES(?,?,?,?)", (uid, "other@example.test", "其他成员", "unused")
    )
    db.execute("INSERT INTO members VALUES(?,?,?)", (uid, row["space_id"], "member"))
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest("other-member-key"), uid, row["space_id"], "api", "test", time.time() + 60),
    )
    headers = {"Authorization": "Bearer other-member-key"}
    assert client.get("/api/agents", headers=headers).status_code == 200
    assert client.get("/api/v1/agents/" + a["id"] + "/runs", headers=headers).json() == []
    assert (
        client.post("/api/v1/runs/" + rid + "/resume", json={}, headers=headers).status_code == 403
    )
    assert client.get("/api/v1/runs/" + rid).json()["status"] == "needs_input"
