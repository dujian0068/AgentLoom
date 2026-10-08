"""Knowledge API endpoints."""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from agentloom import knowledge
from agentloom import store as db
from agentloom.dependencies import auth
from agentloom.schema import EmbeddingResumeInput, KBInput, KBReindexInput, KBSearchInput
from agentloom.security import public
from agentloom.services import embedding, knowledge_imports

router = APIRouter(tags=["knowledge"])


@router.post("/api/wiki")
def create_kb(payload: KBInput, user=Depends(auth)):
    return public(
        db.put_resource(
            user["space_id"],
            "wiki",
            _index_payload(user["space_id"], payload),
        )
    )


def _index_payload(space, payload, *, revision=1):
    result = {
        **payload.model_dump(),
        "revision": 0,
        "status": "empty",
        "retrieval": "hybrid" if payload.embedding_id else "keyword",
    }
    if payload.embedding_id:
        result["embedding_index"] = embedding.freeze_index(
            space,
            payload.embedding_id,
            [binding.model_dump() for binding in payload.embedding_hooks],
            payload.embedding_dimensions,
            revision=revision,
        )
    return result


def _failure(exc):
    return {
        "status": "failed",
        "error": str(exc),
        "import_id": getattr(exc, "import_id", None),
        "operation_id": getattr(exc, "operation_id", None),
        "retry_required": getattr(exc, "retry_required", False),
    }


def _error_response(exc):
    return JSONResponse(
        {"detail": str(exc), **_failure(exc)},
        status_code=409 if getattr(exc, "retry_required", False) else 422,
    )


@router.post("/api/wiki/{rid}/upload")
async def wiki_upload(rid: str, files: list[UploadFile] = File(...), user=Depends(auth)):
    kb = db.resource(rid, user["space_id"], "wiki")
    if kb.get("rebuild_imports") and kb.get("status") != "ready":
        raise ValueError("请先恢复并完成当前知识库索引重建")
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
            result = await knowledge_imports.import_document(
                user["space_id"], kb, name, text, user["user_id"], invoke=knowledge.embeddings
            )
            results.append(result)
        except knowledge_imports.ImportFailure as exc:
            results.append({"name": file.filename, **_failure(exc)})
        except (ValueError, UnicodeError) as e:
            results.append({"name": file.filename, "status": "failed", "error": str(e)})
    return results


@router.get("/api/wiki/{rid}/imports")
def imports(rid: str, user=Depends(auth)):
    return knowledge_imports.list_imports(user["space_id"], rid, user["user_id"])


@router.post("/api/wiki/{rid}/imports/{import_id}/resume")
async def resume_import(
    rid: str, import_id: str, payload: EmbeddingResumeInput, user=Depends(auth)
):
    try:
        return await knowledge_imports.execute_import(
            user["space_id"],
            rid,
            import_id,
            user["user_id"],
            retry_unknown=payload.retry_unknown_models,
            invoke=knowledge.embeddings,
        )
    except knowledge_imports.ImportFailure as exc:
        return _error_response(exc)


@router.get("/api/wiki/{rid}/embedding-operations")
def embedding_operations(rid: str, user=Depends(auth)):
    db.resource(rid, user["space_id"], "wiki")
    return [
        embedding.get_operation(user["space_id"], row["id"], user["user_id"])
        for row in db.query(
            "SELECT id FROM embedding_operations WHERE space_id=? AND kb_id=? AND actor_id=? ORDER BY created DESC",
            (user["space_id"], rid, user["user_id"]),
        )
    ]


async def _execute_rebuild(kb, payload, user):
    results = []
    # Preflight every job so explicit consent is requested before any new call.
    jobs = [
        knowledge_imports.get_import(user["space_id"], kb["id"], job, user["user_id"])
        for job in kb["rebuild_imports"]
    ]
    pending_unknown = next((job for job in jobs if job["retry_required"]), None)
    if pending_unknown and not payload.retry_unknown_models:
        return _error_response(
            knowledge_imports.ImportFailure(
                pending_unknown["import_id"],
                "上次向量化请求结果未知，请明确允许重试后恢复，可能再次计费",
                operation_id=pending_unknown["operation_id"],
                retry_required=True,
            )
        )
    for job in jobs:
        try:
            results.append(
                await knowledge_imports.execute_import(
                    user["space_id"],
                    kb["id"],
                    job["import_id"],
                    user["user_id"],
                    retry_unknown=payload.retry_unknown_models,
                    invoke=knowledge.embeddings,
                )
            )
        except knowledge_imports.ImportFailure as exc:
            results.append({"name": job["name"], **_failure(exc)})
    return {**public(db.resource(kb["id"], user["space_id"], "wiki")), "imports": results}


@router.post("/api/wiki/{rid}/reindex")
async def reindex(rid: str, payload: KBReindexInput, user=Depends(auth)):
    source = db.resource(rid, user["space_id"], "wiki")
    revision = (source.get("embedding_index") or {}).get("revision", 0) + 1
    merged = {
        key: source.get(key) for key in ("embedding_id", "embedding_hooks", "embedding_dimensions")
    }
    merged["embedding_id"] = merged["embedding_id"] or ""
    merged["embedding_hooks"] = merged["embedding_hooks"] or []
    for key in ("embedding_id", "embedding_hooks"):
        if getattr(payload, key) is not None:
            merged[key] = getattr(payload, key)
    if "embedding_dimensions" in payload.model_fields_set:
        merged["embedding_dimensions"] = payload.embedding_dimensions
    merged["name"] = payload.name or (source["name"][:80] + f" · 索引 v{revision}")
    config = KBInput.model_validate(merged)
    target = knowledge_imports.prepare_rebuild(
        user["space_id"],
        source,
        _index_payload(user["space_id"], config, revision=revision),
        user["user_id"],
    )
    return await _execute_rebuild(target, EmbeddingResumeInput(), user)


@router.post("/api/wiki/{rid}/reindex/resume")
async def resume_reindex(rid: str, payload: EmbeddingResumeInput, user=Depends(auth)):
    kb = db.resource(rid, user["space_id"], "wiki")
    if not kb.get("derived_from") or not kb.get("rebuild_imports"):
        raise ValueError("知识库没有可恢复的索引重建任务")
    return await _execute_rebuild(kb, payload, user)


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
async def wiki_search(rid: str, payload: KBSearchInput, user=Depends(auth)):
    query = payload.query.strip()
    if not query:
        raise ValueError("请输入检索内容")
    try:
        return await knowledge.search(
            user["space_id"],
            [db.resource(rid, user["space_id"], "wiki")],
            query,
            actor_id=user["user_id"],
            embedding_operation_id=payload.embedding_operation_id,
            retry_unknown=payload.retry_unknown_models,
        )
    except embedding.EmbeddingOperationError as exc:
        return _error_response(exc)
