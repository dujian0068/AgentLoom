"""Models API endpoints."""

from agentloom_runtime import provider
from fastapi import APIRouter, Depends

from agentloom import store as db
from agentloom.dependencies import auth, owner, valid_url
from agentloom.schema import ModelInput
from agentloom.security import decrypt, encrypt, public

router = APIRouter(tags=["models"])


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
