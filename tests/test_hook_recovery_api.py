"""Recovery never silently repeats an external model request with unknown result."""

import json

from agentloom import store as db
from agentloom.security import decrypt
from agentloom.services import hooks as hook_service
from agentloom_runtime import provider
from agentloom_runtime.hooks import (
    Continue,
    HookDefinition,
    HookRegistry,
)
from test_harness import answer, decision
from test_platform import add_agent, wait_run


def start(client, model, **config):
    agent = add_agent(client, model, **config)
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    response = client.post(f"/api/v1/agents/{agent['id']}/runs", json={"input": "answer once"})
    assert response.status_code == 200
    return response.json()["run_id"]


def checkpoint(rid):
    saved = db.query("SELECT payload FROM checkpoints WHERE run_id=?", (rid,), True)
    return json.loads(decrypt(saved["payload"]))


def test_unknown_model_requires_explicit_api_retry_and_persists_authorization(
    client, model, monkeypatch
):
    action_calls = 0

    async def chat(model, messages, tools, secret):
        nonlocal action_calls
        if not tools:
            return decision()
        action_calls += 1
        if action_calls == 1:
            raise OSError("connection lost after sending request")
        return answer("completed")

    monkeypatch.setattr(provider, "chat", chat)
    rid = start(client, model)
    state = wait_run(client, rid)
    assert state["status"] == "failed" and state["resumable"]
    assert state["requires_model_retry"] is True
    original = checkpoint(rid)
    response = client.post(f"/api/v1/runs/{rid}/resume", json={"input": "not yet accepted"})
    assert response.status_code == 409
    assert "retry_unknown_models" in response.json()["detail"]
    assert checkpoint(rid) == original and action_calls == 1
    assert (
        client.post(f"/api/v1/runs/{rid}/resume", json={"retry_unknown_models": "true"}).status_code
        == 422
    )
    response = client.post(
        f"/api/v1/runs/{rid}/resume", json={"retry_unknown_models": True, "stream": True}
    )
    assert response.status_code == 200 and "run.completed" in response.text
    state = wait_run(client, rid)
    assert state["status"] == "succeeded" and not state["requires_model_retry"]
    assert action_calls == 2
    retried = [
        operation
        for operation in checkpoint(rid)["operations"].values()
        if operation.get("retry_authorizations")
    ]
    assert len(retried) == 1 and retried[0]["retry_authorizations"] == 1
    assert retried[0]["raw_output"]["content"] == "completed"
    assert retried[0]["scope"]["run_id"] == rid
    assert retried[0]["scope"]["actor_id"] == state["user_id"]
    assert retried[0]["scope"]["published_revision"] == state["version"]


def test_received_model_result_after_hook_failure_resumes_without_retry_permission(
    client, model, monkeypatch
):
    action_calls = 0
    after_calls = 0

    async def chat(model, messages, tools, secret):
        nonlocal action_calls
        if not tools:
            return decision()
        action_calls += 1
        return answer("paid response retained")

    async def after(context, payload):
        nonlocal after_calls
        after_calls += 1
        if after_calls == 1:
            raise RuntimeError("temporary output processing failure")
        return Continue()

    monkeypatch.setattr(provider, "chat", chat)
    monkeypatch.setattr(hook_service, "TRUSTED_HOOKS", HookRegistry())
    hook_service.register_trusted_hook(HookDefinition("pure-output", "v1", after, replay_safe=True))
    rid = start(
        client,
        model,
        hooks=[
            {
                "binding_id": "output-binding",
                "hook_id": "pure-output",
                "version": "v1",
                "point": "model.chat.after",
            }
        ],
    )
    state = wait_run(client, rid)
    assert state["status"] == "failed" and state["resumable"]
    assert state["requires_model_retry"] is False
    response = client.post(f"/api/v1/runs/{rid}/resume", json={"stream": True})
    assert response.status_code == 200 and "run.completed" in response.text
    state = wait_run(client, rid)
    assert state["output"] == "paid response retained"
    assert action_calls == 1 and after_calls == 2
    assert not any(op.get("retry_authorizations") for op in checkpoint(rid)["operations"].values())
