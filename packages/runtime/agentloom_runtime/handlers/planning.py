"""Expose plan management through the same capability boundary as tools."""

from ..planning import update_plan
from ..tool_contracts import ToolDefinition, parameters

S = {"type": "string"}
PLAN_ITEM = {
    "type": "object",
    "properties": {
        "id": S,
        "step": S,
        "status": {"type": "string", "enum": ["pending", "in_progress", "completed", "cancelled"]},
    },
    "required": ["id", "step", "status"],
    "additionalProperties": False,
}


async def update(context, args):
    return update_plan(context, context.frame, args, context.instance)


def register(registry, snapshot, *, decrypt, search):
    registry.register(
        ToolDefinition(
            name="update_plan",
            description="创建或更新执行计划。根据实际结果更新状态，可调整步骤；提交完整计划及变更原因。",
            parameters=parameters(
                {
                    "steps": {"type": "array", "items": PLAN_ITEM, "minItems": 1, "maxItems": 12},
                    "explanation": S,
                },
                ["steps", "explanation"],
            ),
            handler=update,
            before_plan=True,
        )
    )
