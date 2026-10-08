"""Adapt each published MCP tool into a registered capability."""

import json
import re

from .. import mcp_tools
from ..tool_contracts import ToolDefinition


def definition(service, spec, decrypt):
    name = "mcp_" + service["id"][:12] + "_" + re.sub("[^A-Za-z0-9_-]", "_", spec["name"])[:35]

    async def invoke(context, args):
        result = await mcp_tools.call(
            service, spec["name"], args, decrypt(service.get("secret", ""))
        )
        if isinstance(result, dict) and result.get("is_error"):
            raise RuntimeError("MCP 工具返回错误：" + json.dumps(result, ensure_ascii=False)[:6000])
        return result

    return ToolDefinition(
        name=name,
        description=spec["description"][:1000],
        parameters=spec["schema"],
        handler=invoke,
        available=lambda config, instance: service["id"] in config["tools"],
        tool_id=f"mcp:{service['id']}:{spec['name']}",
    )


def register(registry, snapshot, *, decrypt, search):
    for service in snapshot["tools"]:
        for spec in service.get("schemas", []):
            registry.register(definition(service, spec, decrypt))
