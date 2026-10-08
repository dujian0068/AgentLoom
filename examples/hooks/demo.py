"""Run a deterministic Hook-enabled Agent without API keys or network calls."""

import asyncio
import json
import tempfile
from copy import deepcopy
from pathlib import Path

from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookManager,
    HookRegistry,
    PatchInput,
    PatchOutput,
)
from agentloom_runtime.runtime import create_engine
from agentloom_runtime.tool_contracts import ToolDefinition, parameters


async def clamp_limit(ctx, call):
    arguments = dict(call["arguments"])
    arguments["limit"] = min(arguments["limit"], ctx.config["max_limit"])
    return PatchInput({"arguments": arguments})


async def public_result(ctx, result):
    return PatchOutput({"value": {"count": result["value"]["count"]}})


async def trim_answer(ctx, response):
    if response["content"] is None:
        return Continue()
    return PatchOutput({"content": response["content"].strip()})


def make_hooks():
    registry = HookRegistry()
    registry.register(
        HookDefinition(
            "clamp-limit",
            "v1",
            clamp_limit,
            replay_safe=True,
            config_schema={
                "type": "object",
                "properties": {"max_limit": {"type": "integer", "minimum": 1}},
                "required": ["max_limit"],
                "additionalProperties": False,
            },
        )
    )
    registry.register(HookDefinition("public-result", "v1", public_result, replay_safe=True))
    registry.register(HookDefinition("trim-answer", "v1", trim_answer, replay_safe=True))
    return HookManager(
        registry,
        [
            HookBinding(
                "limit-search",
                "clamp-limit",
                "tool.before",
                version="v1",
                targets=("orders-search",),
                config={"max_limit": 3},
            ),
            HookBinding(
                "hide-internal-fields",
                "public-result",
                "tool.after",
                version="v1",
                targets=("orders-search",),
            ),
            HookBinding(
                "clean-answer",
                "trim-answer",
                "model.chat.after",
                version="v1",
                purposes=("action",),
            ),
        ],
    )


class DemoGateway:
    module_id = "hook-demo-gateway/v1"

    def __init__(self):
        self.actions = 0

    async def invoke(self, request):
        if request.purpose == "verification":
            return {
                "role": "assistant",
                "content": json.dumps({"decision": "complete", "reason": "demo verified"}),
            }
        self.actions += 1
        if self.actions == 1:
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "search-1",
                        "type": "function",
                        "function": {"name": "orders_search", "arguments": '{"limit": 500}'},
                    }
                ],
            }
        assert json.loads(request.messages[-1]["content"]) == {"count": 3}
        return {"role": "assistant", "content": "  已查询 3 条记录。  "}


def configure_tools(registry):
    async def orders_search(context, arguments):
        assert arguments["limit"] == 3
        return {"count": arguments["limit"], "internal_note": "仅保留在原始操作记录"}

    registry.register(
        ToolDefinition(
            "orders_search",
            "查询订单",
            parameters({"limit": {"type": "integer", "minimum": 1}}, ["limit"]),
            orders_search,
            implementation_id="orders-search/v1",
            tool_id="orders-search",
        )
    )


async def main():
    async def search(*args):
        return []

    snapshot = {
        "config": {
            "prompt": "查询订单并回答",
            "mode": "react",
            "skills": [],
            "tools": [],
            "wiki": [],
            "subs": [],
        },
        "model_obj": {"model_id": "demo-model"},
        "skills": [],
        "tools": [],
        "wiki": [],
    }
    checkpoints = []
    with tempfile.TemporaryDirectory(prefix="agentloom-hooks-") as directory:
        engine = create_engine(
            snapshot,
            Path(directory),
            lambda *args: None,
            lambda value: value,
            search,
            model_gateway=DemoGateway(),
            configure_tools=configure_tools,
            hooks=make_hooks(),
            hook_scope={"run_id": "demo-run", "published_revision": "demo-v1"},
            save=lambda state: checkpoints.append(deepcopy(state)),
        )
        try:
            output = await engine.execute("查询订单")
            assert output == "已查询 3 条记录。"
            operation = next(iter(engine.state["tool_operations"].values()))
            print(output)
            print("实际工具参数:", operation["final_input"]["arguments"])
            print("原始工具结果:", operation["raw_output"]["value"])
            print("模型可见结果:", operation["effective_output"]["value"])
            print("已保存检查点:", len(checkpoints))
        finally:
            await engine.close()


if __name__ == "__main__":
    asyncio.run(main())
