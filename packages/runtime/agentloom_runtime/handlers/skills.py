"""Published Skill instructions, supporting files and sandbox execution."""

from pathlib import Path

from .. import sandbox
from ..tool_contracts import ToolDefinition, bindings, parameters
from .workspace import prepare_workspace, safe_file

S = {"type": "string"}


def register(registry, snapshot, *, decrypt, search):
    def available(config, instance):
        return bool(bindings(snapshot, config, "skills"))

    def bound_skill(context, args):
        skill = next(
            (
                item
                for item in bindings(snapshot, context.config, "skills")
                if item["id"] == args["skill_id"]
            ),
            None,
        )
        if not skill:
            raise ValueError("技能未绑定")
        return skill

    async def load(context, args):
        skill = bound_skill(context, args)
        context.emit("skill.loaded", {"instance": context.instance, "name": skill["name"]})
        return skill["content"]

    async def read(context, args):
        skill = bound_skill(context, args)
        result = safe_file(Path(skill["path"]), args["path"]).read_text()[:20000]
        context.emit("skill.loaded", {"instance": context.instance, "name": skill["name"]})
        return result

    async def command(context, args):
        return await sandbox.command(
            prepare_workspace(context),
            bindings(snapshot, context.config, "skills"),
            args["command"],
        )

    registry.register(
        ToolDefinition(
            name="skill_load",
            description="按需加载已绑定技能详细指令",
            parameters=parameters({"skill_id": S}, ["skill_id"]),
            handler=load,
            available=available,
        )
    )
    registry.register(
        ToolDefinition(
            name="skill_read",
            description="读取技能中的参考资料与脚本",
            parameters=parameters({"skill_id": S, "path": S}, ["skill_id", "path"]),
            handler=read,
            available=available,
        )
    )
    registry.register(
        ToolDefinition(
            name="workspace_command",
            description="在隔离容器执行 shell 命令。工作目录 /workspace；技能在 /skills/<skill_id>。无网络，无平台密钥。",
            parameters=parameters({"command": S}, ["command"]),
            handler=command,
            available=available,
        )
    )
