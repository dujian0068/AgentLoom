"""Create or resume a scoped child Agent while retaining its checkpoint identity."""

from ..tool_contracts import ToolDefinition, parameters

S = {"type": "string"}


def register(registry, snapshot, *, decrypt, search):
    async def delegate(context, args):
        return await context.children.run(args["subagent_id"], args["task"])

    registry.register(
        ToolDefinition(
            name="delegate_task",
            description="根据职责创建子 Agent 执行具体子任务，继承当前模型。传入任务及必要背景。",
            parameters=parameters({"subagent_id": S, "task": S}, ["subagent_id", "task"]),
            handler=delegate,
            available=lambda config, instance: instance == "main" and bool(config.get("subs")),
            resume_inflight=True,
            timeout=600,
        )
    )
