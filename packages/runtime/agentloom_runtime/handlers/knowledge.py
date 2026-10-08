"""Search the current Agent's published knowledge bindings."""

from ..tool_contracts import ToolDefinition, bindings, parameters

S = {"type": "string"}


def register(registry, snapshot, *, decrypt, search):
    contextual_search = getattr(search, "search_with_context", None)
    if contextual_search is not None and not callable(contextual_search):
        raise ValueError("Knowledge search_with_context must be callable")

    async def retrieve(context, args):
        libraries = bindings(snapshot, context.config, "wiki")
        result = (
            await contextual_search(libraries, args["query"], context.invocation)
            if contextual_search is not None
            else await search(libraries, args["query"])
        )
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
            # Only an explicit host adapter promises durable, invocation-scoped
            # embedding IDs. Legacy callables keep the original no-replay policy.
            resume_inflight=contextual_search is not None,
            # The durable host owns its deadline and recovery cleanup. An outer
            # timeout must not consume its cancellation as a completed tool error.
            timeout=None if contextual_search is not None else 90,
            available=lambda config, instance: bool(bindings(snapshot, config, "wiki")),
        )
    )
