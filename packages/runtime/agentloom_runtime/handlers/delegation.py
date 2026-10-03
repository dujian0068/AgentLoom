"""Create or resume a scoped child Agent while retaining its checkpoint identity."""

from ..tool_contracts import ToolDefinition, parameters

S = {"type": "string"}


def register(registry, snapshot, *, decrypt, search):
    async def delegate(context, args):
        child = next(
            (item for item in context.config["subs"] if item["id"] == args["subagent_id"]),
            None,
        )
        if not child:
            raise ValueError("子 Agent 配置不存在")
        child_id = context.pending.get("child_instance")
        if not child_id:
            if context.state["delegations"] >= 8:
                raise ValueError("达到子任务数量限制（8 个）")
            context.state["delegations"] += 1
            child_id = f"sub-{context.state['delegations']}"
            context.pending["child_instance"] = child_id
            context.persist()
            context.emit(
                "subagent.created",
                {
                    "id": child_id,
                    "name": child["name"],
                    "task": args["task"],
                    "model": snapshot["model_obj"]["model_id"],
                },
            )
        try:
            result = await context.run_child(child, args["task"], (), child_id)
        except Exception as error:
            context.emit("subagent.failed", {"id": child_id, "error": str(error)})
            raise
        outcome = context.state["frames"][child_id]["outcome"]
        context.emit(
            "subagent.completed",
            {"id": child_id, "name": child["name"], "output": result, "outcome": outcome},
        )
        return {"status": outcome, "output": result}

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
