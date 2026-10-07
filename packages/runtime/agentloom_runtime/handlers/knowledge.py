"""Search the current Agent's published knowledge bindings."""

from ..tool_contracts import ToolDefinition, bindings, parameters

S = {"type": "string"}


def register(registry, snapshot, *, decrypt, search):
    async def retrieve(context, args):
        libraries = bindings(snapshot, context.config, "wiki")
        result = await search(libraries, args["query"])
        citations = context.citations.record(result)
        context.emit(
            "knowledge.retrieved",
            {
                "instance": context.instance,
                "query": args["query"],
                "citations": citations,
            },
        )
        return result

    registry.register(
        ToolDefinition(
            name="knowledge_search",
            description="检索已绑定知识库，返回可引用的原文片段",
            parameters=parameters({"query": S}, ["query"]),
            handler=retrieve,
            available=lambda config, instance: bool(bindings(snapshot, config, "wiki")),
        )
    )
