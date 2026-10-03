"""Auth API endpoints."""

import secrets
import shutil

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from agentloom import store as db
from agentloom.dependencies import auth, limited_auth, login_token
from agentloom.schema import Login
from agentloom.security import password_hash, password_ok

router = APIRouter(tags=["auth"])


@router.get("/api/health")
def health():
    db.query("SELECT 1 AS connected", one=True)
    return {
        "status": "ok",
        "name": "AgentLoom",
        "database": db.backend(),
        "docker": bool(shutil.which("docker")),
    }


@router.get("/api/auth/status")
def status():
    return {"needs_setup": not bool(db.query("SELECT id FROM users LIMIT 1"))}


@router.post("/api/auth/setup")
def setup(payload: Login, response: Response, request: Request):
    limited_auth(request)
    user_id, space_id = db.uid(), db.uid()
    with db.transaction("setup") as c:
        if c.execute("SELECT id FROM users LIMIT 1").fetchone():
            raise HTTPException(409, "平台已初始化，请登录")
        c.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            (user_id, payload.email.strip().lower(), payload.name, password_hash(payload.password)),
        )
        c.execute(
            "INSERT INTO spaces VALUES(?,?,?)", (space_id, "团队空间", secrets.token_urlsafe(16))
        )
        c.execute("INSERT INTO members VALUES(?,?,?)", (user_id, space_id, "owner"))
    login_token({"user_id": user_id, "space_id": space_id}, response)
    return {"ok": True}


@router.post("/api/auth/register")
def register(payload: Login, response: Response, request: Request):
    limited_auth(request)
    space = db.query("SELECT * FROM spaces WHERE invite=?", (payload.invite,), True)
    if not space:
        raise ValueError("邀请码无效")
    email = payload.email.strip().lower()
    user_id = db.uid()
    with db.transaction("account:" + email) as c:
        if c.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
            raise ValueError("该账号已存在，请登录")
        c.execute(
            "INSERT INTO users VALUES(?,?,?,?)",
            (user_id, email, payload.name, password_hash(payload.password)),
        )
        c.execute("INSERT INTO members VALUES(?,?,?)", (user_id, space["id"], "member"))
    login_token({"user_id": user_id, "space_id": space["id"]}, response)
    return {"ok": True}


@router.post("/api/auth/login")
def login(payload: Login, response: Response, request: Request):
    limited_auth(request)
    user = db.query(
        "SELECT u.*,m.space_id FROM users u JOIN members m ON u.id=m.user_id WHERE email=?",
        (payload.email.strip().lower(),),
        True,
    )
    if not user or not password_ok(payload.password, user["password"]):
        raise HTTPException(401, "账号或密码不正确")
    login_token({"user_id": user["id"], "space_id": user["space_id"]}, response)
    return {"ok": True}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response, user=Depends(auth)):
    db.execute("DELETE FROM tokens WHERE hash=?", (user["hash"],))
    response.delete_cookie("loom_session")
    return {"ok": True}


@router.get("/api/me")
def me(user=Depends(auth)):
    space = db.query("SELECT id,name FROM spaces WHERE id=?", (user["space_id"],), True)
    return {
        "name": user["display_name"],
        "email": user["email"],
        "role": user["role"],
        "space": space,
        "docker": bool(shutil.which("docker")),
    }
