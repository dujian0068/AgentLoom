"""Agents API endpoints."""

import json
import time

from fastapi import APIRouter, Depends, HTTPException

from agentloom import store as db
from agentloom.dependencies import auth
from agentloom.schema import AgentConfig
from agentloom.security import public
from agentloom.services.agents import get_agent, snapshot

router = APIRouter(tags=["agents"])


@router.get("/api/agents")
def agents(user=Depends(auth)):
    return [
        get_agent(x["id"], user["space_id"])
        for x in db.query(
            "SELECT id FROM agents WHERE space_id=? ORDER BY created DESC", (user["space_id"],)
        )
    ]


@router.post("/api/agents")
def create_agent(payload: AgentConfig, user=Depends(auth)):
    aid = db.uid()
    db.execute(
        "INSERT INTO agents VALUES(?,?,?,0,?)",
        (aid, user["space_id"], payload.model_dump_json(), time.time()),
    )
    return get_agent(aid, user["space_id"])


@router.put("/api/agents/{aid}")
def update_agent(aid: str, payload: AgentConfig, user=Depends(auth)):
    get_agent(aid, user["space_id"])
    db.execute(
        "UPDATE agents SET config=? WHERE id=? AND space_id=?",
        (payload.model_dump_json(), aid, user["space_id"]),
    )
    return get_agent(aid, user["space_id"])


@router.post("/api/agents/{aid}/publish")
def publish(aid: str, user=Depends(auth)):
    a = get_agent(aid, user["space_id"])
    config = {k: v for k, v in a.items() if k not in ("id", "published")}
    snap = snapshot(config, user["space_id"])
    with db.transaction("agent:" + aid) as c:
        version = c.execute("SELECT latest FROM agents WHERE id=?", (aid,)).fetchone()[0] + 1
        c.execute(
            "INSERT INTO versions VALUES(?,?,?,?)",
            (aid, version, json.dumps(snap, ensure_ascii=False), time.time()),
        )
        c.execute("UPDATE agents SET latest=? WHERE id=?", (version, aid))
    return {"version": version}


@router.get("/api/agents/{aid}/versions")
def versions(aid: str, user=Depends(auth)):
    get_agent(aid, user["space_id"])
    return db.query(
        "SELECT version,created FROM versions WHERE agent_id=? ORDER BY version DESC", (aid,)
    )


@router.get("/api/agents/{aid}/versions/{version}")
def version_config(aid: str, version: int, user=Depends(auth)):
    get_agent(aid, user["space_id"])
    v = db.query(
        "SELECT snapshot FROM versions WHERE agent_id=? AND version=?", (aid, version), True
    )
    if not v:
        raise HTTPException(404, "版本不存在")
    return public(json.loads(v["snapshot"]))
