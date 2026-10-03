"""Runs API endpoints."""

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from agentloom import store as db
from agentloom.assets import safe_path
from agentloom.dependencies import auth
from agentloom.schema import ResumeInput, RunInput
from agentloom.security import decrypt
from agentloom.services.agents import get_agent
from agentloom.services.runs import (
    events_stream,
    execute_run,
    finish_run,
    flush_finalizations,
    get_run,
)
from agentloom.state import TASKS

router = APIRouter(tags=["runs"])


@router.post("/api/v1/agents/{aid}/runs")
async def run_agent(aid: str, payload: RunInput, user=Depends(auth)):
    a = get_agent(aid, user["space_id"])
    version = payload.version or a["published"]
    if not version:
        raise ValueError("请先发布 Agent 再运行")
    row = db.query(
        "SELECT snapshot FROM versions WHERE agent_id=? AND version=?", (aid, version), True
    )
    if not row:
        raise ValueError("发布版本不存在")
    snap = json.loads(row["snapshot"])
    snap["config"]["version"] = version
    sid = payload.session_id or db.uid()
    rid = db.uid()
    history = []
    with db.transaction("space:" + user["space_id"]) as c:
        if (
            c.execute(
                "SELECT COUNT(*) AS n FROM runs WHERE space_id=? AND status IN ('queued','running')",
                (user["space_id"],),
            ).fetchone()["n"]
            >= 4
        ):
            raise HTTPException(429, "空间同时最多运行 4 个任务")
        if payload.session_id:
            existing = c.execute(
                "SELECT * FROM sessions WHERE id=? AND space_id=? AND user_id=?",
                (sid, user["space_id"], user["user_id"]),
            ).fetchone()
            if not existing or existing["agent_id"] != aid or existing["version"] != version:
                raise ValueError("会话不存在或发布版本不一致")
            if c.execute(
                "SELECT id FROM runs WHERE session_id=? AND status IN ('queued','running')",
                (sid,),
            ).fetchone():
                raise ValueError("该会话正在执行任务")
        else:
            c.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?)",
                (sid, user["space_id"], user["user_id"], aid, version),
            )
        previous = c.execute(
            "SELECT input,output FROM runs WHERE session_id=? AND status='succeeded' ORDER BY created DESC LIMIT 6",
            (sid,),
        ).fetchall()
        for r in reversed(previous):
            history.extend(
                [
                    {"role": "user", "content": r["input"]},
                    {"role": "assistant", "content": r["output"]},
                ]
            )
        db.lock(c, "run:" + rid)
        c.execute(
            "INSERT INTO runs(id,space_id,user_id,agent_id,version,session_id,input,status,created) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                rid,
                user["space_id"],
                user["user_id"],
                aid,
                version,
                sid,
                payload.input,
                "queued",
                time.time(),
            ),
        )
        db._insert_event(
            c, rid, "run.created", {"run_id": rid, "session_id": sid, "version": version}
        )
    TASKS[rid] = asyncio.create_task(execute_run(rid, snap, user["space_id"], history))
    if payload.stream:
        return StreamingResponse(
            events_stream(rid, user),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Run-ID": rid, "X-Session-ID": sid},
        )
    return {"run_id": rid, "session_id": sid, "version": version}


@router.get("/api/v1/runs/{rid}")
def run_state(rid: str, user=Depends(auth)):
    row = get_run(rid, user)
    row["resumable"] = row["status"] in (
        "failed",
        "cancelled",
        "interrupted",
        "needs_input",
    ) and bool(db.query("SELECT run_id FROM checkpoints WHERE run_id=?", (rid,), True))
    row["events"] = [
        {"seq": x["seq"], "kind": x["kind"], **json.loads(x["payload"])}
        for x in db.query("SELECT * FROM events WHERE run_id=? ORDER BY seq", (rid,))
    ]
    workspace = db.DATA / "runs" / rid
    row["artifacts"] = (
        [
            str(p.relative_to(workspace))
            for p in workspace.rglob("*")
            if p.is_file() and not p.is_symlink()
        ]
        if workspace.exists()
        else []
    )
    return row


@router.get("/api/v1/runs/{rid}/events")
async def events(rid: str, request: Request, after: int = 0, user=Depends(auth)):
    try:
        last_id = int(request.headers.get("last-event-id", "0"))
    except ValueError:
        raise ValueError("事件游标格式不正确")
    if after < 0 or last_id < 0:
        raise ValueError("事件游标不能小于零")
    after = max(after, last_id)
    get_run(rid, user)
    return StreamingResponse(
        events_stream(rid, user, after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/api/v1/runs/{rid}/cancel")
async def cancel(rid: str, user=Depends(auth)):
    row = get_run(rid, user)
    if row["status"] in ("queued", "running") and rid in TASKS:
        TASKS[rid].cancel()
        if row["status"] == "queued":
            finish_run(rid, "cancelled", "run.cancelled", {})
            TASKS.pop(rid, None)
    return {"ok": True}


@router.get("/api/v1/agents/{aid}/runs")
def run_history(aid: str, version: int | None = None, user=Depends(auth)):
    get_agent(aid, user["space_id"])
    sql = "SELECT id,version,input,status,created FROM runs WHERE agent_id=? AND space_id=? AND user_id=?"
    args = [aid, user["space_id"], user["user_id"]]
    if version is not None:
        sql += " AND version=?"
        args.append(version)
    return db.query(sql + " ORDER BY created DESC LIMIT 30", args)


@router.post("/api/v1/runs/{rid}/resume")
async def resume_run(rid: str, payload: ResumeInput, user=Depends(auth)):
    row = get_run(rid, user)
    await asyncio.to_thread(flush_finalizations, rid)
    if row["user_id"] != user["user_id"]:
        raise HTTPException(403, "只能恢复自己的任务")
    version = db.query(
        "SELECT snapshot FROM versions WHERE agent_id=? AND version=?",
        (row["agent_id"], row["version"]),
        True,
    )
    snap = json.loads(version["snapshot"])
    snap["config"]["version"] = row["version"]
    # Validate current credentials before changing a terminal run to queued.
    db.resource(snap["model_obj"]["id"], user["space_id"], "models")
    with db.transaction("space:" + user["space_id"]) as c:
        db.lock(c, "run:" + rid)
        current = c.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()["status"]
        if current not in ("failed", "cancelled", "interrupted", "needs_input"):
            raise ValueError("任务正在执行或已经完成，不能恢复")
        if (
            c.execute(
                "SELECT COUNT(*) FROM runs WHERE space_id=? AND status IN ('queued','running')",
                (user["space_id"],),
            ).fetchone()[0]
            >= 4
        ):
            raise HTTPException(429, "空间同时最多运行 4 个任务")
        if c.execute(
            "SELECT id FROM runs WHERE session_id=? AND id<>? AND status IN ('queued','running')",
            (row["session_id"], rid),
        ).fetchone():
            raise ValueError("该会话正在执行另一个任务")
        saved = c.execute("SELECT payload FROM checkpoints WHERE run_id=?", (rid,)).fetchone()
        if not saved:
            raise ValueError("此任务没有可恢复的执行检查点，请新建任务")
        checkpoint = json.loads(decrypt(saved["payload"]))
        c.execute("UPDATE runs SET status='queued',output='',error=NULL WHERE id=?", (rid,))
        after = db._insert_event(c, rid, "run.resumed", {"input": payload.input, "run_id": rid}) - 1
    TASKS[rid] = asyncio.create_task(
        execute_run(rid, snap, user["space_id"], [], checkpoint, payload.input)
    )
    if payload.stream:
        return StreamingResponse(
            events_stream(rid, user, after),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Run-ID": rid,
                "X-Session-ID": row["session_id"],
            },
        )
    return {
        "run_id": rid,
        "session_id": row["session_id"],
        "version": row["version"],
        "after": after,
    }


@router.get("/api/v1/runs/{rid}/artifact")
def artifact(rid: str, path: str, user=Depends(auth)):
    get_run(rid, user)
    p = safe_path(db.DATA / "runs" / rid, path)
    if not p.is_file():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name, media_type="application/octet-stream")
