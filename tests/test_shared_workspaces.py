"""Workspace volume admission, session continuity and API file authorization."""

import json
import time

import pytest
from agentloom import store as db
from agentloom.security import digest
from agentloom.services import workspaces
from agentloom_runtime import provider


@pytest.fixture(autouse=True)
def isolated_workspace_environment(monkeypatch):
    for name in (
        "AGENT_LOOM_WORKSPACE_BACKEND",
        "AGENT_LOOM_WORKSPACE_ROOT",
        "AGENT_LOOM_WORKSPACE_VOLUME_ID",
    ):
        monkeypatch.delenv(name, raising=False)


def new_run(*, session=None, agent="application-a", space=None, owner=None, pin=True):
    member = db.query("SELECT * FROM members LIMIT 1", one=True)
    row = {
        "id": db.uid(),
        "space_id": space or member["space_id"],
        "user_id": owner or member["user_id"],
        "agent_id": agent,
        "session_id": session or db.uid(),
    }
    with db.transaction("test-run") as connection:
        connection.execute(
            "INSERT INTO runs(id,space_id,user_id,agent_id,version,session_id,input,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                row["id"],
                row["space_id"],
                row["user_id"],
                row["agent_id"],
                1,
                row["session_id"],
                "test",
                "succeeded",
                time.time(),
            ),
        )
        if pin:
            workspaces.pin_run(connection, row["id"])
    return db.query("SELECT * FROM runs WHERE id=?", (row["id"],), True)


def put_main(row, name="report.md", content="session file"):
    _, root = workspaces.resolve_run(row)
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def shared_volume(monkeypatch, root, volume="team-volume"):
    root.mkdir()
    (root / ".agentloom-volume").write_text(volume)
    monkeypatch.setenv("AGENT_LOOM_WORKSPACE_BACKEND", "shared_posix")
    monkeypatch.setenv("AGENT_LOOM_WORKSPACE_ROOT", str(root))
    monkeypatch.setenv("AGENT_LOOM_WORKSPACE_VOLUME_ID", volume)


def test_same_session_shares_main_files_but_not_child_instances(client):
    first = new_run()
    put_main(first)
    second = new_run(session=first["session_id"])
    first_provider, first_root = workspaces.resolve_run(first)
    second_provider, second_root = workspaces.resolve_run(second)
    assert first_root == second_root
    assert first_provider.children_root != second_provider.children_root
    child = first_provider.children_root / "sub-1"
    child.mkdir(parents=True)
    (child / "child.txt").write_text("private child")
    assert workspaces.read_artifact(second, "report.md") == b"session file"
    assert "subagents/sub-1/child.txt" in workspaces.list_artifacts(first)
    assert "subagents/sub-1/child.txt" not in workspaces.list_artifacts(second)
    with pytest.raises(FileNotFoundError):
        workspaces.read_artifact(second, "subagents/sub-1/child.txt")


def test_api_multiple_questions_reuse_session_workspace(client, model, monkeypatch):
    actions = [
        ("workspace_write", {"path": "handoff.md", "content": "kept across questions"}),
        None,
        ("workspace_read", {"path": "handoff.md"}),
        None,
    ]

    async def chat(model, messages, tools, secret):
        if "TASK_COMPLETION_REVIEW" in messages[0]["content"]:
            return {
                "role": "assistant",
                "content": json.dumps(
                    {"decision": "complete", "reason": "checked", "next_action": ""}
                ),
            }
        action = actions.pop(0)
        if action is None:
            return {"role": "assistant", "content": "done"}
        name, arguments = action
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-" + str(len(actions)),
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ],
        }

    monkeypatch.setattr(provider, "chat", chat)
    agent = client.post("/api/agents", json={"name": "Session test", "model": model["id"]}).json()
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    endpoint = f"/api/v1/agents/{agent['id']}/runs"
    first = client.post(endpoint, json={"input": "write", "stream": True})
    assert first.status_code == 200
    second = client.post(
        endpoint,
        json={"input": "read", "stream": True, "session_id": first.headers["x-session-id"]},
    )
    assert second.status_code == 200
    result = client.get("/api/v1/runs/" + second.headers["x-run-id"]).json()
    assert result["status"] == "succeeded"
    read = next(
        event
        for event in result["events"]
        if event["kind"] == "tool.completed" and event["name"] == "workspace_read"
    )
    assert read["result"]["content"] == "kept across questions"
    assert result["artifacts"] == ["handoff.md"]
    assert db.query("SELECT COUNT(*) AS n FROM run_workspaces", one=True)["n"] == 2


@pytest.mark.parametrize("changed", ["space_id", "agent_id", "session_id"])
def test_workspaces_are_isolated_by_every_scope(client, changed):
    first = new_run()
    put_main(first)
    args = {"session": first["session_id"], "agent": first["agent_id"], "space": first["space_id"]}
    args[{"space_id": "space", "agent_id": "agent", "session_id": "session"}[changed]] = "other"
    second = new_run(**args)
    assert workspaces.resolve_run(first)[1] != workspaces.resolve_run(second)[1]
    with pytest.raises(FileNotFoundError):
        workspaces.read_artifact(second, "report.md")


def test_shared_volume_identity_is_portable_between_node_mount_paths(client, tmp_path, monkeypatch):
    first_mount = tmp_path / "node-a"
    shared_volume(monkeypatch, first_mount)
    row = new_run()
    first, path_a = workspaces.resolve_run(row)
    second_mount = tmp_path / "node-b"
    shared_volume(monkeypatch, second_mount)
    second, path_b = workspaces.resolve_run(row)
    assert first.binding == second.binding
    assert path_a != path_b
    assert str(first_mount) not in json.dumps(first.binding)
    assert path_a.relative_to(first_mount) == path_b.relative_to(second_mount)
    next_run = new_run(session=row["session_id"])
    assert workspaces.resolve_run(next_run)[1] == path_b


@pytest.mark.parametrize(
    "failure",
    [
        "missing_root",
        "missing_marker",
        "wrong_marker",
        "marker_symlink",
        "relative_root",
        "missing_volume",
    ],
)
def test_shared_mount_fails_closed_without_creating_fallback(
    client, tmp_path, monkeypatch, failure
):
    root = tmp_path / "shared"
    shared_volume(monkeypatch, root)
    marker = root / ".agentloom-volume"
    if failure == "missing_root":
        marker.unlink()
        root.rmdir()
    elif failure == "missing_marker":
        marker.unlink()
    elif failure == "wrong_marker":
        marker.write_text("another-volume")
    elif failure == "marker_symlink":
        marker.unlink()
        target = tmp_path / "fake-marker"
        target.write_text("team-volume")
        marker.symlink_to(target)
    elif failure == "relative_root":
        monkeypatch.setenv("AGENT_LOOM_WORKSPACE_ROOT", "shared")
    else:
        monkeypatch.delenv("AGENT_LOOM_WORKSPACE_VOLUME_ID")
    with pytest.raises(ValueError):
        new_run()
    assert db.query("SELECT COUNT(*) AS n FROM run_workspaces", one=True)["n"] == 0
    assert db.query("SELECT COUNT(*) AS n FROM runs", one=True)["n"] == 0
    if failure == "missing_root":
        assert not root.exists()


def test_existing_session_and_run_cannot_change_volume(client, tmp_path, monkeypatch):
    root = tmp_path / "shared"
    shared_volume(monkeypatch, root)
    row = new_run()
    (root / ".agentloom-volume").write_text("different")
    monkeypatch.setenv("AGENT_LOOM_WORKSPACE_VOLUME_ID", "different")
    with pytest.raises(ValueError, match="存储卷"):
        workspaces.resolve_run(row)
    with pytest.raises(ValueError, match="同一会话"):
        new_run(session=row["session_id"])


def test_running_provider_rechecks_shared_mount_before_file_access(client, tmp_path, monkeypatch):
    root = tmp_path / "shared"
    shared_volume(monkeypatch, root)
    row = new_run()
    provider, _ = workspaces.resolve_run(row)
    (root / ".agentloom-volume").unlink()
    with pytest.raises(ValueError, match="共享"):
        provider.for_instance("main")
    assert not (root / "spaces").exists()


def test_legacy_run_keeps_original_directory(client):
    row = new_run(pin=False)
    target = put_main(row)
    assert target == db.DATA / "runs" / row["id"] / "report.md"
    assert workspaces.resolve_run(row)[0] is None
    assert workspaces.list_artifacts(row) == ["report.md"]
    assert workspaces.read_artifact(row, "report.md") == b"session file"


def test_artifact_api_uses_same_backend_and_rejects_symlink_escape(client, tmp_path):
    row = new_run()
    target = put_main(row)
    endpoint = f"/api/v1/runs/{row['id']}"
    assert client.get(endpoint).json()["artifacts"] == ["report.md"]
    response = client.get(endpoint + "/artifact", params={"path": "report.md"})
    assert response.content == b"session file"
    assert "attachment" in response.headers["content-disposition"]
    outside = tmp_path / "outside-secret"
    outside.write_text("do not read")
    target.unlink()
    target.symlink_to(outside)
    response = client.get(endpoint + "/artifact", params={"path": "report.md"})
    assert response.status_code == 400
    assert "do not read" not in response.text
    assert (
        client.get(endpoint + "/artifact", params={"path": "../../outside-secret"}).status_code
        == 400
    )


def test_other_users_cannot_download_or_enumerate_session_files(client):
    row = new_run()
    put_main(row)
    other = db.uid()
    db.execute("INSERT INTO users VALUES(?,?,?,?)", (other, "other@test", "Other", "unused"))
    db.execute("INSERT INTO members VALUES(?,?,?)", (other, row["space_id"], "member"))
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest("other-token"), other, row["space_id"], "api", "test", time.time() + 100),
    )
    headers = {"Authorization": "Bearer other-token"}
    endpoint = f"/api/v1/runs/{row['id']}"
    assert client.get(endpoint, headers=headers).status_code == 403
    assert client.get(endpoint + "/events", headers=headers).status_code == 403
    assert (
        client.get(
            endpoint + "/artifact", params={"path": "report.md"}, headers=headers
        ).status_code
        == 403
    )
    assert client.post(endpoint + "/cancel", headers=headers).status_code == 403
    assert client.post(endpoint + "/resume", json={}, headers=headers).status_code == 403


def test_binding_cannot_be_reused_for_another_scope(client):
    row = new_run()
    forged = {**row, "agent_id": "different-agent"}
    with pytest.raises(ValueError, match="作用域"):
        workspaces.resolve_run(forged)
