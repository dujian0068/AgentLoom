"""Task-local file capabilities and shared path boundaries."""

from pathlib import Path

from ..tool_contracts import ToolDefinition, parameters

S = {"type": "string"}


def safe_file(root: Path, name: str):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError("路径超出工作区")
    return path


def prepare_workspace(context):
    workspace = context.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    workspace.chmod(0o777)
    return workspace


async def read(context, args):
    workspace = prepare_workspace(context)
    return safe_file(workspace, args["path"]).read_text()[:20000]


async def write(context, args):
    workspace = prepare_workspace(context)
    content = args["content"]
    if len(content.encode()) > 2 * 1024 * 1024:
        raise ValueError("单文件超过 2MB")
    target = safe_file(workspace, args["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    target.chmod(0o666)
    return {"file": str(target.relative_to(context.root_workspace.resolve()))}


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
