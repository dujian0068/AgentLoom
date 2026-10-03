"""Tools API endpoints."""

import asyncio
import shutil

from agentloom_runtime import mcp_tools
from fastapi import APIRouter, Depends, File, Form, UploadFile

from agentloom import store as db
from agentloom.assets import git_import, write_uploads
from agentloom.dependencies import auth, valid_url
from agentloom.schema import GitInput, ToolInput
from agentloom.security import decrypt, encrypt, public
from agentloom.services.tools import do_build, hosted
from agentloom.state import BUILDS

router = APIRouter(tags=["tools"])


@router.post("/api/tools/external")
async def external_tool(payload: ToolInput, user=Depends(auth)):
    value = payload.model_dump(exclude={"api_key"})
    value.update(
        endpoint=valid_url(payload.endpoint),
        secret=encrypt(payload.api_key),
        source="external",
        status="connecting",
        schemas=[],
    )
    try:
        value["schemas"] = await mcp_tools.discover(value, payload.api_key)
        value["status"] = "ready"
    except Exception:
        value.update(status="failed", error="无法发现 MCP 工具，请检查地址、认证和传输方式")
    return public(db.put_resource(user["space_id"], "tools", value))


@router.post("/api/tools/{rid}/refresh")
async def refresh_tool(rid: str, user=Depends(auth)):
    value = db.resource(rid, user["space_id"], "tools")
    payload = {k: v for k, v in value.items() if k not in ("id", "kind")}
    try:
        payload["schemas"] = await mcp_tools.discover(value, decrypt(value.get("secret", "")))
        payload["status"] = "ready"
        payload.pop("error", None)
    except Exception:
        payload.update(status="failed", error="无法发现工具，请检查服务状态")
    return public(db.put_resource(user["space_id"], "tools", payload, rid))


@router.post("/api/tools/{rid}/test")
async def test_tool(rid: str, payload: dict, user=Depends(auth)):
    tool = db.resource(rid, user["space_id"], "tools")
    if payload.get("name") not in [x["name"] for x in tool["schemas"]]:
        raise ValueError("工具不存在")
    try:
        return await mcp_tools.call(
            tool, payload["name"], payload.get("arguments", {}), decrypt(tool.get("secret", ""))
        )
    except Exception:
        raise ValueError("工具调用失败，请检查参数或服务状态")


@router.post("/api/tools/upload")
async def tool_upload(
    name: str = Form(...), files: list[UploadFile] = File(...), user=Depends(auth)
):
    return await hosted(await write_uploads(files), user["space_id"], name)


@router.post("/api/tools/git")
async def tool_git(payload: GitInput, user=Depends(auth)):
    root, _ = await git_import(payload.url, payload.subdir, payload.ref)
    return await hosted(root, user["space_id"], payload.name)


@router.post("/api/tools/{rid}/build")
async def build(rid: str, user=Depends(auth)):
    tool = db.resource(rid, user["space_id"], "tools")
    if tool["source"] != "hosted":
        raise ValueError("外部工具无需构建")
    if rid in BUILDS:
        raise ValueError("工具正在构建")
    if not shutil.which("docker"):
        raise ValueError("当前环境未安装 Docker")
    value = {k: v for k, v in tool.items() if k not in ("id", "kind")}
    value["status"] = "building"
    db.put_resource(user["space_id"], "tools", value, rid)
    BUILDS[rid] = asyncio.create_task(do_build(rid, user["space_id"]))
    return {"ok": True}
