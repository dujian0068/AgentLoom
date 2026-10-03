"""FastAPI assembly: lifecycle, middleware, routers and static frontend."""

import asyncio
import os
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg_pool import PoolTimeout

from . import store as db
from .routes import agents, auth, knowledge, models, resources, runs, skills, spaces, tools
from .services.runs import repair_finalizations
from .state import AUTH_ATTEMPTS as AUTH_ATTEMPTS
from .state import BUILDS, TASKS


@asynccontextmanager
async def lifespan(app):
    repair = None
    try:
        db.init()
        repair = asyncio.create_task(repair_finalizations())
        yield
    finally:
        tasks = [*TASKS.values(), *BUILDS.values()]
        if repair:
            tasks.append(repair)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()


app = FastAPI(
    title="AgentLoom API",
    version="0.2.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)


@app.middleware("http")
async def origin_check(request, call_next):
    origin = request.headers.get("origin")
    if (
        request.method not in ("GET", "HEAD", "OPTIONS")
        and origin
        and request.cookies.get("loom_session")
        and not request.headers.get("authorization")
    ):
        allowed = {
            str(request.base_url).rstrip("/"),
            os.getenv("AGENT_LOOM_DEV_ORIGIN", "http://127.0.0.1:5173"),
        }
        if origin not in allowed:
            return JSONResponse({"detail": "请求来源不合法"}, 403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@app.exception_handler(psycopg.OperationalError)
@app.exception_handler(psycopg.InterfaceError)
@app.exception_handler(PoolTimeout)
async def database_unavailable(request, exc):
    return JSONResponse({"detail": "数据库连接不可用，请检查 SSH 隧道和数据库服务后重试"}, 503)


@app.exception_handler(ValueError)
async def bad_value(request, exc):
    return JSONResponse({"detail": str(exc)}, 400)


@app.exception_handler(RequestValidationError)
async def bad_request(request, exc):
    return JSONResponse(
        {
            "detail": "字段格式不合法："
            + ", ".join(".".join(map(str, e["loc"])) for e in exc.errors())
        },
        422,
    )


for module in (auth, spaces, resources, models, skills, tools, knowledge, agents, runs):
    app.include_router(module.router)

web = db.ROOT / "apps/web/dist"
if web.exists():
    app.mount("/", StaticFiles(directory=web, html=True), name="web")
