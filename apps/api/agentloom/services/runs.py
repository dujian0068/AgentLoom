"""Runs service functions."""

import asyncio
import json
import sqlite3
import threading

import psycopg
from agentloom_runtime.runtime import create_engine
from fastapi import HTTPException
from psycopg_pool import PoolTimeout

from agentloom import store as db
from agentloom.security import decrypt
from agentloom.services.hooks import hook_manager
from agentloom.services.knowledge_search import run_knowledge_search
from agentloom.services.session_history import save_checkpoint
from agentloom.services.workspaces import resolve_run
from agentloom.state import PENDING_FINALIZATIONS, TASKS

_CONNECTION_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError, PoolTimeout)
_FINALIZATIONS_LOCK = threading.RLock()
DATABASE_FAILURE_MESSAGE = "数据库访问异常，任务已停止；请检查数据库连接后恢复任务"


def finish_run(rid, status, kind, payload, output="", error=None):
    """Save a terminal result, retaining it for repair if the connection is lost."""
    pending = {"status": status, "kind": kind, "payload": payload, "output": output, "error": error}
    with _FINALIZATIONS_LOCK:
        try:
            db.finalize(rid, **pending)
        except _CONNECTION_ERRORS:
            PENDING_FINALIZATIONS[rid] = pending
            return False
        PENDING_FINALIZATIONS.pop(rid, None)
        return True


def flush_finalizations(rid=None):
    """Retry idempotent terminal writes, optionally for a run about to resume."""
    completed = 0
    # Hold the lock through each write: a resume flush must not pass an older
    # background retry that could finalize the newly resumed execution.
    with _FINALIZATIONS_LOCK:
        if rid is None:
            pending_items = list(PENDING_FINALIZATIONS.items())
        elif rid in PENDING_FINALIZATIONS:
            pending_items = [(rid, PENDING_FINALIZATIONS[rid])]
        else:
            pending_items = []
        for run_id, pending in pending_items:
            try:
                db.finalize(run_id, **pending)
            except _CONNECTION_ERRORS:
                continue
            PENDING_FINALIZATIONS.pop(run_id, None)
            completed += 1
    return completed


async def repair_finalizations():
    """Repair disconnected terminal writes without blocking the event loop."""
    while True:
        await asyncio.sleep(3)
        worker = asyncio.create_task(asyncio.to_thread(flush_finalizations))
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Shutdown waits for the borrowed connection before closing the pool.
            await worker
            raise


async def execute_run(
    rid,
    snap,
    space,
    history,
    checkpoint=None,
    instruction="",
    history_metadata=None,
    *,
    retry_unknown_models=False,
):
    secrets_in_use = []
    engine = None

    def redact(value):
        text = json.dumps(value, ensure_ascii=False)
        for secret in secrets_in_use:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return json.loads(text)

    def emit(kind, payload):
        db.event(rid, kind, redact(payload))

    try:
        row = db.query("SELECT * FROM runs WHERE id=?", (rid,), True)
        if not row or row["status"] == "cancelled":
            return
        hooks = hook_manager(snap["config"], snap.get("hook_manifest"))
        snap["model_obj"]["secret"] = db.resource(snap["model_obj"]["id"], space, "models")[
            "secret"
        ]
        secrets_in_use.append(decrypt(snap["model_obj"]["secret"]))
        for tool in snap["tools"]:
            secrets_in_use.append(decrypt(tool.get("secret", "")))
        workspace_provider, workspace = resolve_run(row)
        db.transition(
            rid,
            "running",
            "run.started",
            {"version": snap["config"].get("version"), "model": snap["model_obj"]["model_id"]},
        )
        async with asyncio.timeout(600):

            def save(state):
                save_checkpoint(rid, state, redact)

            engine = create_engine(
                snap,
                workspace,
                emit,
                decrypt,
                run_knowledge_search(
                    space,
                    actor_id=row["user_id"],
                    run_id=rid,
                    checkpoint=checkpoint,
                    retry_unknown=retry_unknown_models,
                ),
                checkpoint=checkpoint,
                save=save,
                hooks=hooks,
                workspace_provider=workspace_provider,
                hook_scope={
                    "space_id": space,
                    "actor_id": row["user_id"],
                    "run_id": rid,
                    "published_revision": row["version"],
                },
            )
            if checkpoint:
                engine.resume(instruction, retry_unknown_models=retry_unknown_models)
            output = redact(await engine.execute(row["input"], history, history_metadata))
            status = "needs_input" if engine.blocked else "succeeded"
            finish_run(
                rid,
                status,
                "run.blocked" if engine.blocked else "run.completed",
                {"output": output, "citations": list(engine.citations.values())},
                output=output,
            )
    except asyncio.CancelledError:
        finish_run(rid, "cancelled", "run.cancelled", {})
        raise
    except Exception as exc:
        error = redact(
            DATABASE_FAILURE_MESSAGE
            if isinstance(exc, (psycopg.Error, sqlite3.Error, PoolTimeout))
            else "任务超过 10 分钟超时"
            if isinstance(exc, TimeoutError)
            else str(exc).strip() or "任务初始化或执行异常，请检查模型、工具及平台密钥文件"
        )
        finish_run(rid, "failed", "run.failed", {"error": error}, error=error)
    finally:
        try:
            if engine is not None:
                await engine.close()
        finally:
            TASKS.pop(rid, None)


def get_run(rid, user):
    row = db.query("SELECT * FROM runs WHERE id=? AND space_id=?", (rid, user["space_id"]), True)
    if not row:
        raise HTTPException(404, "运行不存在")
    if row["user_id"] != user["user_id"]:
        raise HTTPException(403, "只能访问自己的会话运行")
    return row


def requires_model_retry(checkpoint):
    """Unknown external requests need explicit authorization to risk another bill."""
    return any(
        operation.get("kind") == "model.chat"
        and "raw_output" not in operation
        and operation.get("actual_status") in {"running", "unknown"}
        for operation in checkpoint.get("operations", {}).values()
    )


async def events_stream(rid, user, after=0):
    seq = after
    while True:
        rows = db.query("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, seq))
        for row in rows:
            seq = row["seq"]
            data = {"seq": seq, "kind": row["kind"], **json.loads(row["payload"])}
            yield "id: " + str(seq) + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"
        status = get_run(rid, user)["status"]
        if status not in ("queued", "running"):
            if db.query(
                "SELECT seq FROM events WHERE run_id=? AND seq>? LIMIT 1", (rid, seq), True
            ):
                continue
            break
        if not rows:
            yield ": heartbeat\n\n"
        await asyncio.sleep(0.3)
