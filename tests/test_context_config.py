import json

import pytest
from agentloom import store as db
from agentloom.schema import AgentConfig, ContextPolicy, ModelInput
from pydantic import ValidationError


@pytest.mark.parametrize(
    "policy",
    [
        {"context_ratio": 0},
        {"context_ratio": 1.01},
        {"target_ratio": 0.8},
        {"target_ratio": -1},
        {"context_ratio": True},
        {"user_turns": 0},
        {"model_steps": 1.5},
        {"model_steps": True},
        {"max_compaction_calls": 17},
        {"unknown": 1},
    ],
)
def test_context_policy_rejects_invalid_limits(policy):
    with pytest.raises(ValidationError):
        ContextPolicy(**policy)


@pytest.mark.parametrize(
    "budget",
    [
        {"context_window": 4096},
        {"max_output_tokens": 0},
        {"max_output_tokens": True},
        {"safety_margin_tokens": -1},
        {"context_window": 32768.5},
        {"metadata": {"provider_max_output_tokens": 100}},
        {"metadata": {"chat_compatible": False}},
    ],
)
def test_model_budget_rejects_invalid_reserves(budget):
    with pytest.raises(ValidationError):
        ModelInput(
            name="test",
            provider="openai",
            model_id="test",
            base_url="https://example.test/v1",
            **budget,
        )


def test_policy_and_budget_freeze_at_publication(client, model):
    config = AgentConfig(name="上下文测试", model=model["id"]).model_dump()
    created = client.post("/api/agents", json=config).json()
    aid = created["id"]
    assert created["context_policy"] == {
        "context_ratio": 0.8,
        "target_ratio": 0.6,
        "user_turns": None,
        "model_steps": None,
        "max_compaction_calls": 4,
    }
    assert client.post(f"/api/agents/{aid}/publish").status_code == 200
    config["context_policy"].update(context_ratio=0.9, user_turns=12, model_steps=20)
    assert client.put(f"/api/agents/{aid}", json=config).status_code == 200
    assert (
        client.put(
            f"/api/models/{model['id']}",
            json={
                **model,
                "context_window": 65536,
                "max_output_tokens": 8192,
            },
        ).status_code
        == 200
    )
    original = client.get(f"/api/agents/{aid}/versions/1").json()
    assert original["config"]["context_policy"]["context_ratio"] == 0.8
    assert original["model_obj"]["context_window"] == 32768
    assert original["model_obj"]["max_output_tokens"] == 4096
    assert client.post(f"/api/agents/{aid}/publish").json()["version"] == 2
    updated = client.get(f"/api/agents/{aid}/versions/2").json()
    assert updated["config"]["context_policy"]["user_turns"] == 12
    assert updated["model_obj"]["context_window"] == 65536


def test_legacy_snapshot_is_unchanged_when_draft_opts_into_policy(client, model):
    config = AgentConfig(name="旧版本", model=model["id"]).model_dump()
    aid = client.post("/api/agents", json=config).json()["id"]
    client.post(f"/api/agents/{aid}/publish")
    row = db.query("SELECT snapshot FROM versions WHERE agent_id=?", (aid,), True)
    original = json.loads(row["snapshot"])
    original["config"].pop("context_policy")
    for field in ("context_window", "max_output_tokens", "safety_margin_tokens", "metadata"):
        original["model_obj"].pop(field)
    encoded = json.dumps(original)
    db.execute("UPDATE versions SET snapshot=? WHERE agent_id=?", (encoded, aid))
    legacy = dict(config)
    legacy.pop("context_policy")
    db.execute("UPDATE agents SET config=? WHERE id=?", (json.dumps(legacy), aid))
    assert "context_policy" not in client.get("/api/agents").json()[0]
    saved = client.put(f"/api/agents/{aid}", json=legacy).json()
    assert saved["context_policy"]["context_ratio"] == 0.8
    assert (
        db.query("SELECT snapshot FROM versions WHERE agent_id=?", (aid,), True)["snapshot"]
        == encoded
    )


def test_child_cannot_override_context_policy(client, model):
    child = {"id": "child", "name": "子任务", "description": "执行任务", "context_policy": {}}
    response = client.post(
        "/api/agents", json={"name": "test", "model": model["id"], "subs": [child]}
    )
    assert response.status_code == 422


def test_publication_freezes_effective_budget_of_legacy_model(client, model):
    resource = db.query("SELECT payload FROM resources WHERE id=?", (model["id"],), True)
    payload = json.loads(resource["payload"])
    for key in ("context_window", "max_output_tokens", "safety_margin_tokens", "metadata"):
        payload.pop(key)
    db.execute("UPDATE resources SET payload=? WHERE id=?", (json.dumps(payload), model["id"]))
    aid = client.post("/api/agents", json={"name": "legacy model", "model": model["id"]}).json()[
        "id"
    ]
    assert client.post(f"/api/agents/{aid}/publish").status_code == 200
    published = client.get(f"/api/agents/{aid}/versions/1").json()["model_obj"]
    assert published["context_window"] == 32768
    assert published["max_output_tokens"] == 4096
