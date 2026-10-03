"""Pass register as create_engine(..., configure_tools=register)."""

from agentloom_runtime.tool_contracts import ToolDefinition, parameters


async def count_text(context, arguments):
    text = arguments["text"]
    return {"characters": len(text), "lines": len(text.splitlines())}


def register(registry):
    registry.register(
        ToolDefinition(
            name="text_count",
            description="统计提供文本的字符数与行数",
            parameters=parameters({"text": {"type": "string"}}, ["text"]),
            handler=count_text,
            timeout=5,
        )
    )
