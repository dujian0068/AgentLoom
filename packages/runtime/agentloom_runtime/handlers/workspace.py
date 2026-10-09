"""Task-local file capabilities and shared path boundaries."""

from pathlib import Path

from ..tool_contracts import ToolDefinition, parameters
from ..workspace import storage_call
from ..workspace_store import PosixWorkspaceStore

S = {"type": "string"}


def safe_file(root: Path, name: str):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("路径超出工作区")
    return path


def prepare_workspace(context):
    return PosixWorkspaceStore(context.workspace).prepare()


async def read(context, args):
    result = await storage_call(
        PosixWorkspaceStore(context.workspace).read, args["path"], limit=1000
    )
    return result["content"]


async def write(context, args):
    result = await storage_call(
        PosixWorkspaceStore(context.workspace).write, args["path"], args["content"]
    )
    path = result["file"]
    if context.instance != "main":
        path = "subagents/" + context.instance + "/" + path
    return {"file": path}


def register(registry, snapshot, *, decrypt, search):
    registry.register(
        ToolDefinition(
            name="workspace_read",
            description="读取本任务工作区文本文件",
            parameters=parameters({"path": S}, ["path"]),
            handler=read,
            evidence_fields=("path",),
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace_write",
            description="生成本任务工作区文件，供用户下载",
            parameters=parameters({"path": S, "content": S}, ["path", "content"]),
            handler=write,
            evidence_fields=("path",),
        )
    )
