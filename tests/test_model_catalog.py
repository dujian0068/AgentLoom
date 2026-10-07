import asyncio
import json

import httpx
import pytest
from agentloom import store as db
from agentloom.services import model_catalog

KEY = "model-discovery-test-secret"


def transport(monkeypatch, handler):
    client_factory = httpx.AsyncClient

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return client_factory(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(model_catalog.httpx, "AsyncClient", factory)


def test_provider_metadata_and_fallbacks_are_explicit(monkeypatch):
    def handler(request):
        assert request.url == "https://api.deepseek.com/models"
        assert request.headers["Authorization"] == "Bearer " + KEY
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "deepseek-flash",
                        "context_window": 1048576,
                        "max_output_tokens": 393216,
                        "output_modalities": ["text"],
                    },
                    {"id": "unknown", "context_window": True},
                    {"id": "echo-" + KEY},
                    {"id": "valid", "name": KEY},
                ]
            },
        )

    transport(monkeypatch, handler)
    result = asyncio.run(model_catalog.discover("deepseek", "https://api.deepseek.com", KEY))
    by_id = {value["id"]: value for value in result["models"]}
    known = by_id["deepseek-flash"]
    assert known["context_window"] == 1048576
    assert known["max_output_tokens"] == 4096
    assert known["metadata"]["provider_max_output_tokens"] == 393216
    assert known["metadata"]["source"] == "provider"
    assert by_id["unknown"]["context_window"] == 32768
    assert by_id["unknown"]["metadata"]["source"] == "platform_default"
    assert KEY not in json.dumps(result)


def test_openai_manifest_exact_ids_and_protocol_support():
    describe = lambda model, base="https://api.openai.com/v1": model_catalog.describe_model(
        {"id": model}, "openai", base, KEY
    )
    assert describe("gpt-4.1-mini")["context_window"] == 1047576
    known = describe("gpt-4o")
    assert known["metadata"]["source"] == "official_manifest"
    assert known["metadata"]["source_url"].endswith("/gpt-4o")
    assert known["metadata"]["verified_at"] == "2026-10-07"
    assert (
        describe("gpt-4o", "https://proxy.example.test/v1")["metadata"]["source"]
        == "platform_default"
    )
    assert describe("gpt-4o-fake")["metadata"]["source"] == "platform_default"
    assert describe("gpt-5.4-pro")["metadata"]["chat_compatible"] is False
    assert describe("text-embedding-3-small")["purpose"] == "embedding"
    assert describe("whisper-1")["metadata"]["chat_compatible"] is False


@pytest.mark.parametrize("status", [302, 401, 403, 429, 500])
def test_provider_errors_do_not_echo_credentials(monkeypatch, status):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(status, text=KEY, headers={"Location": "https://other.example.test/"})

    transport(monkeypatch, handler)
    with pytest.raises(ValueError) as caught:
        asyncio.run(model_catalog.discover("openai", "https://api.openai.com/v1", KEY))
    assert KEY not in str(caught.value)
    assert seen == ["https://api.openai.com/v1/models"]


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"data": []}', b"x" * (1024 * 1024 + 1)])
def test_invalid_or_empty_catalog_is_actionable(monkeypatch, body):
    transport(monkeypatch, lambda request: httpx.Response(200, content=body))
    with pytest.raises(ValueError):
        asyncio.run(model_catalog.discover("openai", "https://api.openai.com/v1", KEY))


def test_discovery_is_owner_only_and_reuses_key_only_for_same_endpoint(client, model, monkeypatch):
    calls = []

    async def discover(provider, base_url, secret):
        calls.append((provider, base_url, secret))
        return {"models": [{"id": "test"}]}

    monkeypatch.setattr(model_catalog, "discover", discover)
    payload = {"provider": "openai", "base_url": model["base_url"], "resource_id": model["id"]}
    response = client.post("/api/models/discover", json=payload)
    assert response.status_code == 200
    assert calls[-1][2] == "unit-test-secret"
    assert "unit-test-secret" not in response.text
    assert (
        client.post(
            "/api/models/discover", json={**payload, "base_url": "https://other.example.test"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/models/discover", json={**payload, "provider": "deepseek"}).status_code
        == 400
    )
    assert len(calls) == 1
    assert (
        client.put(
            f"/api/models/{model['id']}", json={**model, "base_url": "https://other.example.test"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/models/discover",
            json={**payload, "base_url": "https://other.example.test", "api_key": KEY},
        ).status_code
        == 200
    )
    assert calls[-1][2] == KEY
    db.execute("UPDATE members SET role='member'")
    assert client.post("/api/models/discover", json=payload).status_code == 403


def test_discovery_cannot_reuse_resource_from_other_space(client, model, monkeypatch):
    async def forbidden(*args):
        raise AssertionError("must not call provider")

    monkeypatch.setattr(model_catalog, "discover", forbidden)
    db.execute("UPDATE resources SET space_id=? WHERE id=?", ("another-space", model["id"]))
    response = client.post(
        "/api/models/discover",
        json={"provider": "openai", "base_url": model["base_url"], "resource_id": model["id"]},
    )
    assert response.status_code == 400
