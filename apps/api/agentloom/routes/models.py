"""Models API endpoints."""

from urllib.parse import urlparse

from agentloom_runtime import provider
from fastapi import APIRouter, Depends

from agentloom import store as db
from agentloom.dependencies import auth, owner, valid_url
from agentloom.schema import ModelDiscoveryInput, ModelInput
from agentloom.security import decrypt, encrypt, public
from agentloom.services import model_catalog

router = APIRouter(tags=["models"])


@router.post("/api/models/discover")
async def discover_models(payload: ModelDiscoveryInput, user=Depends(auth)):
    owner(user)
    base_url = valid_url(payload.base_url)
    if urlparse(base_url).query or urlparse(base_url).fragment:
        raise ValueError("模型基础地址不能包含查询参数或片段")
    secret = payload.api_key
    if not secret and payload.resource_id:
        old = db.resource(payload.resource_id, user["space_id"], "models")
        if old["provider"] != payload.provider or old["base_url"].rstrip("/") != base_url:
            raise ValueError("更换供应商或服务地址后，请重新填写 API Key")
        secret = decrypt(old["secret"])
    if secret and secret in base_url:
        raise ValueError("模型基础地址不能包含 API Key")
    return await model_catalog.discover(payload.provider, base_url, secret)


@router.post("/api/models")
def create_model(payload: ModelInput, user=Depends(auth)):
    owner(user)
    if not payload.api_key:
        raise ValueError("请输入 API Key")
    value = payload.model_dump(exclude={"api_key"})
    value.update(
        base_url=valid_url(payload.base_url), secret=encrypt(payload.api_key), status="configured"
    )
    return public(db.put_resource(user["space_id"], "models", value))


@router.put("/api/models/{rid}")
def update_model(rid: str, payload: ModelInput, user=Depends(auth)):
    owner(user)
    old = db.resource(rid, user["space_id"], "models")
    if not payload.api_key and (
        old["provider"] != payload.provider
        or old["base_url"].rstrip("/") != valid_url(payload.base_url)
    ):
        raise ValueError("更换供应商或服务地址后，请重新填写 API Key")
    if any(x.get("embedding_id") == rid for x in db.list_resources(user["space_id"], "wiki")) and (
        payload.model_id != old["model_id"]
        or payload.base_url.rstrip("/") != old["base_url"]
        or payload.purpose != "embedding"
    ):
        raise ValueError(
            "该模型用于知识索引，只能更新名称或 Key；更换向量模型请创建新知识库重新索引"
        )
    value = payload.model_dump(exclude={"api_key"})
    value.update(
        base_url=valid_url(payload.base_url),
        secret=encrypt(payload.api_key) if payload.api_key else old["secret"],
        status="configured",
    )
    return public(db.put_resource(user["space_id"], "models", value, rid))


@router.post("/api/models/{rid}/test")
async def test_model(rid: str, user=Depends(auth)):
    owner(user)
    model = db.resource(rid, user["space_id"], "models")
    try:
        if model["purpose"] == "embedding":
            await provider.embeddings(model, ["连接测试"], decrypt(model["secret"]))
        else:
            await provider.chat(
                model, [{"role": "user", "content": "请回复 OK"}], [], decrypt(model["secret"])
            )
    except Exception as e:
        raise ValueError(str(e))
    return {"ok": True}
