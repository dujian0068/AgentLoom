"""Spaces API endpoints."""

import secrets
import time

from fastapi import APIRouter, Depends

from agentloom import store as db
from agentloom.dependencies import auth, owner
from agentloom.security import digest

router = APIRouter(tags=["spaces"])


@router.get("/api/members")
def members(user=Depends(auth)):
    return db.query(
        "SELECT u.id,u.name,u.email,m.role FROM users u JOIN members m ON u.id=m.user_id WHERE m.space_id=?",
        (user["space_id"],),
    )


@router.get("/api/invite")
def invite(user=Depends(auth)):
    owner(user)
    return {
        "invite": db.query("SELECT invite FROM spaces WHERE id=?", (user["space_id"],), True)[
            "invite"
        ]
    }


@router.post("/api/api-keys")
def api_key(payload: dict, user=Depends(auth)):
    owner(user)
    key = "loom_" + secrets.token_urlsafe(32)
    name = str(payload.get("name", "API 客户端"))[:100]
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (digest(key), user["user_id"], user["space_id"], "api", name, time.time() + 90 * 86400),
    )
    return {"key": key, "name": name, "expires_days": 90}


@router.get("/api/api-keys")
def keys(user=Depends(auth)):
    owner(user)
    return db.query(
        "SELECT hash AS id,name,expires FROM tokens WHERE space_id=? AND kind='api'",
        (user["space_id"],),
    )


@router.delete("/api/api-keys/{key_id}")
def revoke(key_id: str, user=Depends(auth)):
    owner(user)
    db.execute(
        "DELETE FROM tokens WHERE hash=? AND space_id=? AND kind='api'", (key_id, user["space_id"])
    )
    return {"ok": True}
