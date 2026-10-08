"""Durable file imports: finish every embedding batch before committing a document."""

import asyncio
import json
import threading
import time
from copy import deepcopy

from agentloom_runtime.tool_contracts import ToolPersistenceError

from agentloom import store as db
from agentloom.security import decrypt, encrypt

from . import embedding

_ACTIVE = set()
_LOCK = threading.Lock()


class ImportFailure(RuntimeError):
    def __init__(self, import_id, message, *, operation_id=None, retry_required=False):
        self.import_id = import_id
        self.operation_id = operation_id
        self.retry_required = retry_required
        super().__init__(message)


class _RebuildValidation(ValueError):
    """A deliberately user-visible validation message owned by this service."""


def _load(space, kb_id, import_id, actor_id=None):
    embedding._authorize(space, actor_id)
    db.resource(kb_id, space, "wiki")
    row = db.query(
        "SELECT * FROM knowledge_imports WHERE id=? AND space_id=? AND kb_id=?",
        (import_id, space, kb_id),
        True,
    )
    if not row or (actor_id is not None and row["actor_id"] != actor_id):
        raise ValueError("文件导入任务不存在或无权访问")
    try:
        payload = json.loads(decrypt(row["payload"]))
    except Exception:
        raise ToolPersistenceError("文件导入记录无法读取") from None
    return row, payload


def _write(import_id, payload, status):
    try:
        if (
            db.execute(
                "UPDATE knowledge_imports SET status=?,payload=?,updated=? WHERE id=?",
                (status, encrypt(json.dumps(payload, ensure_ascii=False)), time.time(), import_id),
            )
            != 1
        ):
            raise ValueError("missing import")
    except Exception:
        raise ToolPersistenceError("文件导入检查点保存失败") from None


def _recovery(row, payload):
    """An import may stop before recording a batch's unknown provider result."""
    failed = payload.get("failed_operation")
    if row["status"] == "ready":
        return None, False
    for operation_id in payload["batches"]:
        if not db.query(
            "SELECT id FROM embedding_operations WHERE id=? AND space_id=? AND kb_id=?",
            (operation_id, row["space_id"], row["kb_id"]),
            True,
        ):
            continue
        operation = embedding.get_operation(row["space_id"], operation_id, row["actor_id"] or None)
        if operation["retry_required"]:
            return operation_id, True
        if operation["status"] != "succeeded":
            failed = operation_id
    return failed, False


def metadata(row, payload):
    operation_id, retry_required = _recovery(row, payload)
    return {
        "import_id": row["id"],
        "name": payload["name"],
        "id": payload["document_id"] if row["status"] == "ready" else None,
        "status": row["status"],
        "operation_id": operation_id,
        "retry_required": retry_required,
        "error": payload.get("error"),
        "created": row["created"],
    }


def get_import(space, kb_id, import_id, actor_id=None):
    return metadata(*_load(space, kb_id, import_id, actor_id))


def list_imports(space, kb_id, actor_id):
    embedding._authorize(space, actor_id)
    db.resource(kb_id, space, "wiki")
    return [
        get_import(space, kb_id, row["id"], actor_id)
        for row in db.query(
            "SELECT id FROM knowledge_imports WHERE space_id=? AND kb_id=? AND actor_id=? ORDER BY created DESC",
            (space, kb_id, actor_id),
        )
    ]


def _import_payload(kb, name, text):
    from agentloom.knowledge import split_text

    if type(text) is not str or not text.strip():
        raise ValueError("文件内容为空")
    if type(name) is not str or not name.strip():
        raise ValueError("文件名不能为空")
    chunks = list(split_text(text))
    return {
        "name": name,
        "text": text,
        "document_id": db.uid(),
        "embedding_id": kb.get("embedding_id", ""),
        "embedding_index": deepcopy(kb.get("embedding_index")),
        "batches": [db.uid() for _ in range(0, len(chunks), 32)] if kb.get("embedding_id") else [],
    }


def create_import(space, kb, name, text, actor_id=None):
    embedding._authorize(space, actor_id)
    current = db.resource(kb["id"], space, "wiki")
    if current.get("embedding_index") != kb.get("embedding_index") or current.get(
        "embedding_id"
    ) != kb.get("embedding_id"):
        raise ValueError("知识库索引配置已变化")
    payload = _import_payload(kb, name, text)
    import_id, now = db.uid(), time.time()
    try:
        db.execute(
            "INSERT INTO knowledge_imports VALUES(?,?,?,?,?,?,?,?)",
            (
                import_id,
                space,
                kb["id"],
                actor_id or "",
                "pending",
                encrypt(json.dumps(payload, ensure_ascii=False)),
                now,
                now,
            ),
        )
    except Exception:
        raise ToolPersistenceError("文件导入记录创建失败") from None
    return import_id


def prepare_rebuild(space, source_kb, new_payload, actor_id):
    """Create the new KB and its complete import manifest in one transaction."""
    embedding._authorize(space, actor_id)
    try:
        return _prepare_rebuild(space, source_kb, new_payload, actor_id)
    except (_RebuildValidation, ToolPersistenceError):
        raise
    except Exception:
        raise ToolPersistenceError("重建知识库的导入记录保存失败") from None


def _prepare_rebuild(space, source_kb, new_payload, actor_id):
    kb_id = db.uid()
    target = deepcopy(new_payload)
    for field in ("id", "kind", "embedding_dimension"):
        target.pop(field, None)
    target.update(derived_from=source_kb["id"], rebuild_imports=[], status="building", revision=0)
    now = time.time()
    with db.transaction("resource:" + source_kb["id"]) as connection:
        row = connection.execute(
            "SELECT payload FROM resources WHERE id=? AND space_id=? AND kind='wiki'",
            (source_kb["id"], space),
        ).fetchone()
        if row is None:
            raise _RebuildValidation("知识库不存在或无权访问")
        source = json.loads(row["payload"])
        if source.get("embedding_index") != source_kb.get("embedding_index") or source.get(
            "embedding_id", ""
        ) != source_kb.get("embedding_id", ""):
            raise _RebuildValidation("来源知识库索引配置已变化")
        if source.get("status") == "building" or any(
            not connection.execute(
                "SELECT id FROM knowledge_imports WHERE id=? AND space_id=? AND kb_id=? AND status='ready'",
                (import_id, space, source_kb["id"]),
            ).fetchone()
            for import_id in source.get("rebuild_imports", [])
        ):
            raise _RebuildValidation("来源知识库尚未完整构建，不能重建部分索引")
        documents = connection.execute(
            "SELECT name,content FROM documents WHERE kb_id=? AND space_id=? AND deleted=0 ORDER BY revision,id",
            (source_kb["id"], space),
        ).fetchall()
        if not documents:
            raise _RebuildValidation("知识库没有可重建的文档")
        for document in documents:
            import_id = db.uid()
            payload = _import_payload(target, document["name"], document["content"])
            target["rebuild_imports"].append(import_id)
            connection.execute(
                "INSERT INTO knowledge_imports VALUES(?,?,?,?,?,?,?,?)",
                (
                    import_id,
                    space,
                    kb_id,
                    actor_id or "",
                    "pending",
                    encrypt(json.dumps(payload, ensure_ascii=False)),
                    now,
                    now,
                ),
            )
        connection.execute(
            "INSERT INTO resources(id,space_id,kind,payload,created) VALUES(?,?,?,?,?)",
            (kb_id, space, "wiki", json.dumps(target, ensure_ascii=False), now),
        )
    return db.resource(kb_id, space, "wiki")


def _commit(space, kb_id, import_id, payload, chunks, vectors, signature):
    from agentloom.knowledge import terms

    dimension = len(vectors[0]) if vectors and vectors[0] is not None else None
    if len(vectors) != len(chunks) or (
        dimension is not None and any(len(vector) != dimension for vector in vectors)
    ):
        raise ValueError("文档向量的数量或批次维度不一致")
    with db.transaction("resource:" + kb_id) as c:
        row = c.execute(
            "SELECT payload FROM resources WHERE id=? AND space_id=? AND kind='wiki'",
            (kb_id, space),
        ).fetchone()
        if row is None:
            raise ValueError("知识库不存在或无权访问")
        current = json.loads(row["payload"])
        if (
            current.get("embedding_index") != payload["embedding_index"]
            or current.get("embedding_id", "") != payload["embedding_id"]
        ):
            raise ValueError("知识库索引配置已变化，不能提交旧向量")
        if current.get("embedding_dimension") not in (None, dimension):
            raise ValueError("向量维度与已有知识索引不一致，请重建索引")
        did = payload["document_id"]
        if not c.execute("SELECT id FROM documents WHERE id=?", (did,)).fetchone():
            revision = current.get("revision", 0) + 1
            c.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?,?,0)",
                (did, kb_id, space, payload["name"], payload["text"], revision),
            )
            for (start, end, content), vector in zip(chunks, vectors):
                cid = db.uid()
                c.execute(
                    "INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        cid,
                        did,
                        kb_id,
                        space,
                        payload["name"],
                        content,
                        start,
                        end,
                        revision,
                        json.dumps(vector) if vector is not None else None,
                        signature,
                    ),
                )
                c.execute("INSERT INTO chunk_fts VALUES(?,?)", (cid, terms(content)))
            current.update(
                revision=revision,
                retrieval="hybrid" if signature else "keyword",
                embedding_dimension=dimension,
            )
        payload.pop("error", None)
        payload.pop("failed_operation", None)
        payload["retry_required"] = False
        c.execute(
            "UPDATE knowledge_imports SET status='ready',payload=?,updated=? WHERE id=? AND space_id=?",
            (encrypt(json.dumps(payload, ensure_ascii=False)), time.time(), import_id, space),
        )
        imports = current.get("rebuild_imports", [])
        pending = any(
            not c.execute(
                "SELECT id FROM knowledge_imports WHERE id=? AND status='ready'", (item,)
            ).fetchone()
            for item in imports
        )
        current["status"] = "building" if pending else "ready"
        c.execute(
            "UPDATE resources SET payload=? WHERE id=? AND space_id=?",
            (json.dumps(current, ensure_ascii=False), kb_id, space),
        )


async def execute_import(
    space, kb_id, import_id, actor_id=None, *, retry_unknown=False, invoke=None
):
    from agentloom.knowledge import split_text

    if type(retry_unknown) is not bool:
        raise ValueError("retry_unknown 必须是布尔值")
    key = (str(db.DB), space, import_id)
    with _LOCK:
        if key in _ACTIVE:
            raise ImportFailure(import_id, "文件导入正在执行")
        _ACTIVE.add(key)
    payload = None
    try:
        row, payload = _load(space, kb_id, import_id, actor_id)
        if row["status"] == "ready":
            return metadata(row, payload)
        kb = db.resource(kb_id, space, "wiki")
        if (
            kb.get("embedding_index") != payload["embedding_index"]
            or kb.get("embedding_id", "") != payload["embedding_id"]
        ):
            raise ValueError("文件导入绑定的索引配置已变化")
        operation_id, retry_required = _recovery(row, payload)
        if retry_required and not retry_unknown:
            raise ImportFailure(
                import_id,
                "向量化调用结果未知；确认可能重复计费后可显式重试",
                operation_id=operation_id,
                retry_required=True,
            )
        _write(import_id, payload, "running")
        chunks = list(split_text(payload["text"]))
        vectors, signature = [None] * len(chunks), ""
        if kb.get("embedding_id"):
            signature = embedding.index_signature(space, kb)
            vectors = []
            for offset in range(0, len(chunks), 32):
                batch, _ = await embedding.embed(
                    space,
                    kb,
                    [chunk[2] for chunk in chunks[offset : offset + 32]],
                    purpose="document",
                    actor_id=row["actor_id"] or None,
                    operation_id=payload["batches"][offset // 32],
                    retry_unknown=retry_unknown,
                    invoke=invoke,
                )
                vectors.extend(batch)
        _commit(space, kb_id, import_id, payload, chunks, vectors, signature)
        return get_import(space, kb_id, import_id, actor_id)
    except asyncio.CancelledError:
        if payload is not None:
            payload["error"] = "文件导入中断，可恢复原任务"
            try:
                _write(import_id, payload, "interrupted")
            except ToolPersistenceError:
                pass
        raise
    except ToolPersistenceError:
        raise
    except Exception as exc:
        if payload is None:
            raise
        payload["error"] = (
            str(exc)
            if isinstance(exc, (ImportFailure, embedding.EmbeddingOperationError))
            else "文件导入未完成，请检查知识库配置或服务状态后恢复原任务"
        )
        payload["failed_operation"] = getattr(exc, "operation_id", None)
        payload["retry_required"] = getattr(exc, "retry_required", False)
        _write(import_id, payload, "failed")
        raise ImportFailure(
            import_id,
            payload["error"],
            operation_id=payload["failed_operation"],
            retry_required=payload["retry_required"],
        ) from None
    finally:
        with _LOCK:
            _ACTIVE.discard(key)


async def import_document(space, kb, name, text, actor_id=None, *, invoke=None):
    import_id = create_import(space, kb, name, text, actor_id)
    return await execute_import(space, kb["id"], import_id, actor_id, invoke=invoke)
