"""Run document/query Embedding Hooks without a model key, database or network."""

import asyncio
import json
from copy import deepcopy

from agentloom_runtime.embedding_hooks import EmbeddingHookBoundary
from agentloom_runtime.hooks import (
    Continue,
    HookBinding,
    HookDefinition,
    HookManager,
    HookRegistry,
    PatchInput,
)


async def trim_text(ctx, payload):
    return PatchInput(
        {
            "texts": [
                {"index": item["index"], "text": item["text"].strip()} for item in payload["texts"]
            ]
        }
    )


async def observe_vectors(ctx, payload):
    assert payload["dimension"] == 2
    assert ctx.kb_id == "demo-wiki"
    return Continue()


async def fake_provider(texts):
    # Returning indexed vectors in a different order is valid provider behavior.
    # The boundary restores input order after retaining the raw provider fact.
    positions = list(reversed(range(len(texts))))
    return {
        "indices": positions,
        "vectors": [[float(len(texts[index])), 1.0] for index in positions],
        "usage": {"total_tokens": sum(len(text) for text in texts)},
    }


async def main():
    registry = HookRegistry()
    registry.register(HookDefinition("trim-text", "v1", trim_text, replay_safe=True))
    registry.register(
        HookDefinition("observe-vectors", "v1", observe_vectors, replay_safe=True, observation=True)
    )
    manager = HookManager(
        registry,
        [
            HookBinding("trim", "trim-text", "model.embedding.before", version="v1"),
            HookBinding("observe", "observe-vectors", "model.embedding.after", version="v1"),
        ],
    )
    boundary = EmbeddingHookBoundary(
        manager,
        model_id="demo-embedding",
        index_signature="demo-index-v1",
        dimensions=2,
        scope={"kb_id": "demo-wiki", "index_revision": 1},
    )
    results = {}
    try:
        for purpose, texts in (("document", ["  hello  ", " wiki "]), ("query", [" hello "])):
            state, checkpoints = {}, []
            result = await boundary.invoke(
                texts,
                fake_provider,
                purpose=purpose,
                state=state,
                save=lambda: checkpoints.append(deepcopy(state)),
            )
            assert state["stage"] == "completed" and checkpoints
            assert [item["text"] for item in state["final_input"]["texts"]] == [
                text.strip() for text in texts
            ]
            results[purpose] = result
    finally:
        await manager.aclose()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
