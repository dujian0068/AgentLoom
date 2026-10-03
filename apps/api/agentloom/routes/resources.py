"""Resources API endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from agentloom import store as db
from agentloom.dependencies import auth, owner
from agentloom.security import public

router = APIRouter(tags=["resources"])


@router.get("/api/resources/{kind}")
def resources(kind: str, user=Depends(auth)):
    if kind not in ("models", "skills", "tools", "wiki"):
        raise HTTPException(404)
    return public(db.list_resources(user["space_id"], kind))


@router.delete("/api/resources/{rid}")
def remove_resource(rid: str, user=Depends(auth)):
    db.resource(rid, user["space_id"])
    if db.resource(rid, user["space_id"])["kind"] == "models":
        owner(user)
    for row in db.query("SELECT config FROM agents WHERE space_id=?", (user["space_id"],)):
        if rid in row["config"]:
            raise ValueError("资源仍被 Agent 草稿引用，请先解除绑定")
    for row in db.query(
        "SELECT v.snapshot FROM versions v JOIN agents a ON a.id=v.agent_id WHERE a.space_id=?",
        (user["space_id"],),
    ):
        if rid in row["snapshot"]:
            raise ValueError("资源仍被发布版本引用，不能删除")
    if any(x.get("embedding_id") == rid for x in db.list_resources(user["space_id"], "wiki")):
        raise ValueError("资源被知识库用于向量化")
    with db.transaction("resource:" + rid) as c:
        c.execute("DELETE FROM resources WHERE id=? AND space_id=?", (rid, user["space_id"]))
    return {"ok": True}
