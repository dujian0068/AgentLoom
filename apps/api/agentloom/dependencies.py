"""Authentication, scope checks and request validation shared by routes."""

import os
import secrets
import time
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from agentloom import store as db
from agentloom.security import digest
from agentloom.state import AUTH_ATTEMPTS


def auth(request: Request):
    header = request.headers.get("authorization", "")
    bearer = header.startswith("Bearer ")
    token = header[7:] if bearer else request.cookies.get("loom_session", "")
    row = db.query(
        "SELECT t.*,u.name AS display_name,u.email,m.role FROM tokens t JOIN users u ON t.user_id=u.id JOIN members m ON m.user_id=t.user_id AND m.space_id=t.space_id WHERE hash=? AND expires>? AND kind=?",
        (digest(token), time.time(), "api" if bearer else "session"),
        True,
    )
    if not row:
        raise HTTPException(401, "请先登录或使用有效的空间 API Key")
    return row


def owner(user):
    if user["role"] != "owner":
        raise HTTPException(403, "仅空间管理员可执行此操作")


def login_token(user, response):
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO tokens VALUES(?,?,?,?,?,?)",
        (
            digest(token),
            user["user_id"],
            user["space_id"],
            "session",
            "网页会话",
            time.time() + 7 * 86400,
        ),
    )
    response.set_cookie(
        "loom_session",
        token,
        httponly=True,
        samesite="strict",
        secure=os.getenv("AGENT_LOOM_SECURE_COOKIE") == "1",
        max_age=7 * 86400,
    )


def valid_url(url):
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password:
        raise ValueError("请提供不含凭据的 HTTP(S) 地址")
    if u.path.endswith("/chat/completions"):
        raise ValueError("请填写接口基础地址，而不是 chat/completions 地址")
    return url.rstrip("/")


def limited_auth(request):
    key = request.client.host if request.client else "local"
    times = [t for t in AUTH_ATTEMPTS.get(key, []) if t > time.time() - 60]
    if len(times) >= 10:
        raise HTTPException(429, "登录尝试过多，请一分钟后重试")
    times.append(time.time())
    AUTH_ATTEMPTS[key] = times
