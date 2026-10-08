"""Published API bindings use trusted deployed code and immutable Hook manifests."""

import pytest
from agentloom.services import hooks as service
from agentloom_runtime import provider
from agentloom_runtime.hooks import HookDefinition, HookRegistry, PatchOutput
from test_harness import answer, decision
from test_platform import add_agent, config, wait_run


def install(monkeypatch, handler, version="v1"):
    monkeypatch.setattr(service, "TRUSTED_HOOKS", HookRegistry())
    definition = HookDefinition(
        "answer-prefix",
        version,
        handler,
        config_schema={
            "type": "object",
            "properties": {"prefix": {"type": "string"}},
            "required": ["prefix"],
            "additionalProperties": False,
        },
        replay_safe=True,
    )
    service.register_trusted_hook(definition)
    return definition


def binding(prefix="published:"):
    return {
        "binding_id": "answer-view",
        "hook_id": "answer-prefix",
        "point": "model.chat.after",
        "config": {"prefix": prefix},
    }


def start(client, aid, version=1):
    response = client.post(
        f"/api/v1/agents/{aid}/runs", json={"input": "answer", "version": version}
    )
    assert response.status_code == 200
    return wait_run(client, response.json()["run_id"])


def test_published_binding_executes_and_draft_or_new_versions_do_not_change_it(
    client, model, monkeypatch
):
    calls = []

    async def prefix(ctx, reply):
        calls.append((ctx.run_id, ctx.actor_id, ctx.published_revision))
        return PatchOutput({"content": ctx.config["prefix"] + reply["content"]})

    definition = install(monkeypatch, prefix)

    async def chat(model, messages, tools, secret):
        return answer("actual response") if tools else decision()

    monkeypatch.setattr(provider, "chat", chat)
    agent = add_agent(client, model, hooks=[binding()])
    response = client.post(f"/api/agents/{agent['id']}/publish")
    assert response.status_code == 200
    snapshot = client.get(f"/api/agents/{agent['id']}/versions/1").json()
    frozen = snapshot["hook_manifest"]["config"]["bindings"][0]
    assert frozen["version"] == "v1" and frozen["code_hash"].startswith("sha256:")
    assert frozen["config"] == {"prefix": "published:"}
    assert "handler" not in str(snapshot["hook_manifest"])
    assert (
        client.put(
            f"/api/agents/{agent['id']}", json=config(model, hooks=[binding("draft:")])
        ).status_code
        == 200
    )
    # The original draft omitted its version. Adding v2 must not make the saved
    # publication ambiguous or silently select the new code.
    service.register_trusted_hook(
        HookDefinition(
            definition.hook_id,
            "v2",
            prefix,
            config_schema=definition.config_schema,
            replay_safe=True,
        )
    )
    result = start(client, agent["id"])
    assert result["status"] == "succeeded"
    assert result["output"] == "published:actual response"
    assert calls == [(result["id"], result["user_id"], 1)]
    current = client.get(f"/api/agents/{agent['id']}/versions/1").json()
    assert current["hook_manifest"] == snapshot["hook_manifest"]


def test_runtime_rejects_deployed_code_drift_before_model_is_called(client, model, monkeypatch):
    async def original(ctx, reply):
        return PatchOutput({"content": ctx.config["prefix"] + reply["content"]})

    async def changed(ctx, reply):
        return PatchOutput({"content": "different implementation"})

    install(monkeypatch, original)
    agent = add_agent(client, model, hooks=[binding()])
    assert client.post(f"/api/agents/{agent['id']}/publish").status_code == 200
    install(monkeypatch, changed)
    model_calls = []

    async def chat(model, messages, tools, secret):
        model_calls.append("called")
        return answer("must not be sent")

    monkeypatch.setattr(provider, "chat", chat)
    result = start(client, agent["id"])
    assert result["status"] == "failed"
    assert "Hook" in result["error"] and "代码或配置已变化" in result["error"]
    assert model_calls == []


def test_publish_rejects_unknown_hooks_and_api_does_not_accept_executable_sources(
    client, model, monkeypatch
):
    monkeypatch.setattr(service, "TRUSTED_HOOKS", HookRegistry())
    agent = add_agent(client, model, hooks=[binding()])
    response = client.post(f"/api/agents/{agent['id']}/publish")
    assert response.status_code == 400 and "Hook 绑定不可用" in response.json()["detail"]
    assert client.get(f"/api/agents/{agent['id']}/versions").json() == []
    for source in ({"entry": "untrusted.py:hook"}, {"git_url": "https://example.test/hook.git"}):
        response = client.post("/api/agents", json=config(model, hooks=[{**binding(), **source}]))
        assert response.status_code == 422


def test_legacy_snapshots_accept_empty_hooks_only():
    assert service.hook_manager({}, None).checkpoint_config()["bindings"] == []
    with pytest.raises(ValueError, match="缺少 Hook 清单"):
        service.hook_manager({"hooks": [binding()]}, None)
