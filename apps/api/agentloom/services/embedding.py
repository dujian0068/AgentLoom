"""Frozen knowledge indexes and encrypted, recoverable embedding operations.

The active-operation guard protects this application's existing single-process
deployment. It is deliberately not a distributed lease or an exactly-once claim.
"""

import asyncio
import hashlib
import json
import threading
import time
from copy import deepcopy

from agentloom_runtime import provider
from agentloom_runtime.embedding_hooks import EmbeddingHookBoundary
from agentloom_runtime.hooks import HookRecoveryRequired
from agentloom_runtime.tool_contracts import ToolPersistenceError

from agentloom import store as db
from agentloom.schema import HookBindingInput
from agentloom.security import decrypt, encrypt

from .hooks import hook_manager, hook_manifest

_ACTIVE = set()
_ACTIVE_LOCK = threading.Lock()
_POINTS = {
    "model.embedding.before",
    "model.embedding.after",
    "operation.error",
    "operation.finally",
}


class EmbeddingOperationError(RuntimeError):
    def __init__(self, operation_id, message, *, retry_required=False, status="failed"):
        self.operation_id = operation_id
        self.retry_required = retry_required
        self.status = status
        super().__init__(message)


def _public_model(model):
    if model.get("purpose") != "embedding":
        raise ValueError("请选择向量化模型连接")
    return {key: model[key] for key in ("id", "provider", "base_url", "model_id")}


def _signature(index):
    content = {key: value for key, value in index.items() if key != "signature"}
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    )


def freeze_index(space, embedding_id, embedding_hooks=(), dimensions=None, *, revision=1):
    """Pin provider identity, dimensions and the same Hook chain for both sides."""
    if dimensions is not None and (type(dimensions) is not int or not 1 <= dimensions <= 65536):
        raise ValueError("向量维度必须是 1 到 65536 的整数")
    if type(revision) is not int or revision < 1:
        raise ValueError("向量索引版本必须是正整数")
    model = db.resource(embedding_id, space, "models")
    public_model = _public_model(model)
    bindings = []
    for value in embedding_hooks:
        binding = HookBindingInput.model_validate(value).model_dump()
        if binding["point"] not in _POINTS:
            raise ValueError("知识库只支持 Embedding 与操作诊断 Hook")
        if binding["purposes"] is not None and set(binding["purposes"]) != {"document", "query"}:
            raise ValueError("知识库 Hook 必须同时覆盖 document 和 query")
        if "main" not in binding["instances"]:
            raise ValueError("知识库 Hook 必须覆盖 main 实例")
        if binding["targets"] and model["model_id"] not in binding["targets"]:
            raise ValueError("知识库 Hook 的目标必须包含绑定的向量模型")
        bindings.append(binding)
    manager = hook_manager({"hooks": bindings})
    manifest = hook_manifest(manager)
    resolved = {entry["binding_id"]: entry["version"] for entry in manifest["config"]["bindings"]}
    for binding in bindings:
        binding["version"] = resolved[binding["binding_id"]]
    index = {
        "revision": revision,
        "model": public_model,
        "dimensions": dimensions,
        "hooks": bindings,
        "hook_manifest": manifest,
    }
    index["signature"] = _signature(index)
    return index


def _validate_index(index, identity):
    if (
        type(index) is not dict
        or set(index) != {"revision", "model", "dimensions", "hooks", "hook_manifest", "signature"}
        or type(index["revision"]) is not int
        or index["revision"] < 1
        or index["signature"] != _signature(index)
    ):
        raise ValueError("知识库向量索引配置损坏，请重建索引")
    if index["model"] != identity:
        raise ValueError("向量模型连接已变化，请恢复原模型配置或重建知识库索引")


def index_signature(space, kb):
    """Read the grouping signature without constructing a Hook executor."""
    db.resource(kb["id"], space, "wiki")
    model = db.resource(kb["embedding_id"], space, "models")
    identity = _public_model(model)
    index = kb.get("embedding_index")
    if index is None:
        if kb.get("embedding_hooks"):
            raise ValueError("知识库缺少冻结的 Embedding Hook 清单，请重建索引")
        return model["base_url"].rstrip("/") + "|" + model["model_id"]
    _validate_index(index, identity)
    return index["signature"]


def resolve_index(space, kb):
    """Resolve pinned code/current credentials without silently changing an index."""
    # A published snapshot can pin an older index, but never confer access to a
    # deleted or foreign resource. Callers retain the corresponding document set.
    db.resource(kb["id"], space, "wiki")
    model = db.resource(kb["embedding_id"], space, "models")
    identity = _public_model(model)
    index = deepcopy(kb.get("embedding_index"))
    if index is None:
        if kb.get("embedding_hooks"):
            raise ValueError("知识库缺少冻结的 Embedding Hook 清单，请重建索引")
        manager = hook_manager({"hooks": []}, None)
        index = {
            "revision": 0,
            "legacy": True,
            "model": identity,
            "dimensions": None,
            "hooks": [],
            "hook_manifest": hook_manifest(manager),
            "signature": model["base_url"].rstrip("/") + "|" + model["model_id"],
        }
        return index, model, manager
    _validate_index(index, identity)
    manager = hook_manager({"hooks": index["hooks"]}, index["hook_manifest"])
    return index, model, manager


def _authorize(space, actor_id, run_id=None):
    if actor_id is not None and not db.query(
        "SELECT 1 FROM members WHERE space_id=? AND user_id=?", (space, actor_id), True
    ):
        raise ValueError("无权访问此空间的向量化操作")
    if run_id is not None:
        run = db.query("SELECT user_id FROM runs WHERE id=? AND space_id=?", (run_id, space), True)
        if not run or (actor_id is not None and run["user_id"] != actor_id):
            raise ValueError("无权访问此任务的向量化操作")


def _read(space, operation_id, actor_id=None):
    _authorize(space, actor_id)
    row = db.query(
        "SELECT * FROM embedding_operations WHERE id=? AND space_id=?", (operation_id, space), True
    )
    if not row or (actor_id is not None and row["actor_id"] not in (None, actor_id)):
        raise ValueError("向量化操作不存在或无权访问")
    db.resource(row["kb_id"], space, "wiki")
    try:
        payload = json.loads(decrypt(row["payload"]))
    except Exception:
        raise ToolPersistenceError("向量化操作记录无法读取") from None
    return row, payload


def _retry_required(state):
    return "raw_output" not in state and state.get("actual_status") in {
        "running",
        "unknown",
        "failed",
        "succeeded",
    }


def _metadata(row, payload):
    output = payload["state"].get("effective_output", {})
    return {
        **{
            key: row[key]
            for key in ("id", "kb_id", "purpose", "index_signature", "status", "created", "updated")
        },
        "operation_id": row["id"],
        "dimensions": output.get("dimension"),
        "retry_required": _retry_required(payload["state"]),
    }


def get_operation(space, operation_id, actor_id=None):
    """Expose recovery metadata only, never source texts, vectors or secrets."""
    return _metadata(*_read(space, operation_id, actor_id))


def requires_run_retry(space, run_id):
    """Read only the run's durable model facts when presenting recovery consent."""
    for row in db.query(
        "SELECT payload FROM embedding_operations WHERE space_id=? AND run_id=?",
        (space, run_id),
    ):
        try:
            state = json.loads(decrypt(row["payload"]))["state"]
        except Exception:
            raise ToolPersistenceError("向量化操作记录无法读取") from None
        if _retry_required(state):
            return True
    return False


def _write(operation_id, payload, status):
    try:
        encrypted = encrypt(json.dumps(payload, ensure_ascii=False, allow_nan=False))
        count = db.execute(
            "UPDATE embedding_operations SET payload=?,status=?,updated=? WHERE id=?",
            (encrypted, status, time.time(), operation_id),
        )
        if count != 1:
            raise ValueError()
    except ToolPersistenceError:
        raise
    except Exception:
        raise ToolPersistenceError("向量化操作检查点保存失败") from None


async def embed(
    space,
    kb,
    texts,
    *,
    purpose,
    actor_id=None,
    run_id=None,
    operation_id=None,
    retry_unknown=False,
    invoke=None,
):
    """Persist before dispatch and before after Hooks; recover with the same ID."""
    if purpose not in {"document", "query"}:
        raise ValueError("向量化用途必须是 document 或 query")
    if type(retry_unknown) is not bool:
        raise ValueError("retry_unknown 必须是布尔值")
    if (
        type(texts) is not list
        or not texts
        or any(type(text) is not str or not text.strip() for text in texts)
    ):
        raise ValueError("向量化输入必须是非空文本列表")
    _authorize(space, actor_id, run_id)
    operation_id = operation_id or db.uid()
    if type(operation_id) is not str or not 1 <= len(operation_id) <= 200:
        raise ValueError("向量化操作 ID 不合法")
    key = (str(db.DB), space, operation_id)
    with _ACTIVE_LOCK:
        if key in _ACTIVE:
            raise EmbeddingOperationError(operation_id, "向量化操作正在执行", status="busy")
        _ACTIVE.add(key)
    manager = None
    try:
        index, model, manager = resolve_index(space, kb)
        existing = db.query("SELECT id FROM embedding_operations WHERE id=?", (operation_id,), True)
        identity = {
            "texts": deepcopy(texts),
            "index": index,
            "kb_id": kb["id"],
            "actor_id": actor_id,
            "run_id": run_id,
            "purpose": purpose,
        }
        if existing:
            row, payload = _read(space, operation_id, actor_id)
            if any(payload.get(name) != value for name, value in identity.items()):
                raise ValueError("恢复操作的输入、身份或索引配置与原记录不一致")
        else:
            payload = {**identity, "state": {"operation_id": operation_id}}
            now = time.time()
            try:
                db.execute(
                    "INSERT INTO embedding_operations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id,
                        space,
                        kb["id"],
                        actor_id,
                        run_id,
                        purpose,
                        index["signature"],
                        "pending",
                        encrypt(json.dumps(payload, ensure_ascii=False)),
                        now,
                        now,
                    ),
                )
            except Exception:
                raise ToolPersistenceError("向量化操作记录创建失败") from None
        state = payload["state"]

        def save():
            status = "succeeded" if state.get("stage") == "completed" else "running"
            _write(operation_id, payload, status)

        async def action(effective_texts):
            secret = decrypt(model["secret"])
            current_model = {
                **model,
                **({"embedding_dimensions": index["dimensions"]} if index["dimensions"] else {}),
            }
            result = await (invoke or provider.embedding_response)(
                current_model, effective_texts, secret
            )
            if isinstance(result, list):
                return {"vectors": result, "indices": list(range(len(result)))}
            return result

        boundary = EmbeddingHookBoundary(
            manager,
            model_id=index["model"]["model_id"],
            index_signature=index["signature"],
            dimensions=index["dimensions"],
            expected_dimensions=kb.get("embedding_dimension"),
            scope={
                "space_id": space,
                "actor_id": actor_id,
                "run_id": run_id,
                "kb_id": kb["id"],
                "index_revision": index["revision"],
                "index_signature": index["signature"],
                "operation_id": operation_id,
            },
        )
        try:
            output = await boundary.invoke(
                texts, action, state=state, save=save, purpose=purpose, retry_unknown=retry_unknown
            )
        except asyncio.CancelledError:
            try:
                _write(operation_id, payload, "cancelled")
            except ToolPersistenceError:
                pass
            raise
        except ToolPersistenceError:
            raise
        except Exception as error:
            retry = _retry_required(state)
            status = (
                "recovery_required"
                if retry or isinstance(error, HookRecoveryRequired)
                else "failed"
            )
            _write(operation_id, payload, status)
            message = (
                "向量化调用结果未知；确认可能重复计费后可显式重试"
                if retry
                else "向量化或 Hook 执行未完成，请检查扩展后恢复原操作"
            )
            raise EmbeddingOperationError(
                operation_id, message, retry_required=retry, status=status
            ) from None
        return output["vectors"], operation_id
    finally:
        try:
            if manager is not None:
                await manager.aclose()
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(key)


async def resume_operation(space, operation_id, actor_id=None, *, retry_unknown=False, invoke=None):
    row, payload = _read(space, operation_id, actor_id)
    kb = db.resource(row["kb_id"], space, "wiki")
    vectors, _ = await embed(
        space,
        kb,
        payload["texts"],
        purpose=row["purpose"],
        actor_id=payload["actor_id"],
        run_id=payload["run_id"],
        operation_id=operation_id,
        retry_unknown=retry_unknown,
        invoke=invoke,
    )
    return vectors, get_operation(space, operation_id, actor_id)
