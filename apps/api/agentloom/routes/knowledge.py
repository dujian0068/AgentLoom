"""Knowledge API endpoints."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from agentloom import store as db
from agentloom.dependencies import auth
from agentloom.knowledge import add_document, search
from agentloom.schema import KBInput
from agentloom.security import public

router = APIRouter(tags=["knowledge"])


@router.post("/api/wiki")
def create_kb(payload: KBInput, user=Depends(auth)):
    if payload.embedding_id:
        model = db.resource(payload.embedding_id, user["space_id"], "models")
        if model["purpose"] != "embedding":
            raise ValueError("请选择向量化模型")
    return public(
        db.put_resource(
            user["space_id"],
            "wiki",
            {
                **payload.model_dump(),
                "revision": 0,
                "status": "empty",
                "retrieval": "hybrid" if payload.embedding_id else "keyword",
            },
        )
    )


@router.post("/api/wiki/{rid}/upload")
async def wiki_upload(rid: str, files: list[UploadFile] = File(...), user=Depends(auth)):
    kb = db.resource(rid, user["space_id"], "wiki")
    results = []
    for file in files:
        try:
            name = Path(file.filename or "").name
            if Path(name).suffix.lower() not in (".md", ".markdown", ".txt", ".sql"):
                raise ValueError("仅支持 Markdown、TXT、SQL 文本")
            blob = await file.read(5 * 1024 * 1024 + 1)
            if len(blob) > 5 * 1024 * 1024:
                raise ValueError("单文件不能超过 5MB")
            try:
                text = blob.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = blob.decode("gb18030")
            kb = db.resource(rid, user["space_id"], "wiki")
            did = await add_document(user["space_id"], kb, name, text)
            results.append({"name": name, "id": did, "status": "ready"})
        except Exception as e:
            results.append({"name": file.filename, "status": "failed", "error": str(e)})
    return results


@router.get("/api/wiki/{rid}/documents")
def documents(rid: str, user=Depends(auth)):
    db.resource(rid, user["space_id"], "wiki")
    return db.query(
        "SELECT id,name,revision FROM documents WHERE kb_id=? AND space_id=? AND deleted=0",
        (rid, user["space_id"]),
    )


@router.get("/api/documents/{did}")
def document(did: str, user=Depends(auth)):
    doc = db.query(
        "SELECT id,name,content FROM documents WHERE id=? AND space_id=? AND deleted=0",
        (did, user["space_id"]),
        True,
    )
    if not doc:
        raise HTTPException(404, "文档不存在")
    return doc


@router.delete("/api/documents/{did}")
def delete_document(did: str, user=Depends(auth)):
    db.execute("UPDATE documents SET deleted=1 WHERE id=? AND space_id=?", (did, user["space_id"]))
    return {"ok": True}


@router.post("/api/wiki/{rid}/search")
async def wiki_search(rid: str, payload: dict, user=Depends(auth)):
    query = str(payload.get("query", "")).strip()[:1000]
    if not query:
        raise ValueError("请输入检索内容")
    return await search(user["space_id"], [db.resource(rid, user["space_id"], "wiki")], query)
