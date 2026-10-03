"""Tools service functions."""

import asyncio
import shutil

from agentloom_runtime import mcp_tools

from agentloom import store as db
from agentloom.security import public
from agentloom.state import BUILDS


async def do_build(rid, space):
    tool = db.resource(rid, space, "tools")
    payload = {k: v for k, v in tool.items() if k not in ("id", "kind")}
    try:
        payload["build_log"] = await mcp_tools.build_tool(tool["path"], tool["image"])
        payload["schemas"] = await mcp_tools.discover(tool)
        payload["status"] = "ready"
        payload.pop("error", None)
    except Exception as e:
        payload.update(status="failed", error=str(e))
    finally:
        db.put_resource(space, "tools", payload, rid)
        BUILDS.pop(rid, None)


async def hosted(root, space, name):
    if not (root / "tool.py").is_file():
        found = list(root.rglob("tool.py"))
        if len(found) != 1:
            raise ValueError("工具目录需要且仅有一个 tool.py")
        root = found[0].parent
    rid = db.uid()
    payload = {
        "name": name or root.name,
        "source": "hosted",
        "path": str(root),
        "image": "agentloom-tool-" + rid,
        "status": "building" if shutil.which("docker") else "environment_missing",
        "schemas": [],
        "build_log": "",
    }
    value = db.put_resource(space, "tools", payload, rid)
    if shutil.which("docker"):
        BUILDS[rid] = asyncio.create_task(do_build(rid, space))
    return public(value)
