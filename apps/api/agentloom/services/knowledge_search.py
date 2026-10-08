"""Bridge a tool's persistent invocation state to recoverable knowledge queries."""

import asyncio
import math
from copy import deepcopy
from uuid import uuid4

from agentloom_runtime.hooks import HookRecoveryRequired

from agentloom import knowledge

from .embedding import EmbeddingOperationError

_STATE_KEY = "knowledge_search"


def run_knowledge_search(space, *, actor_id, run_id, checkpoint=None, retry_unknown=False):
    """Keep old checkpoints on their original non-replayable search contract."""
    adapter = RunKnowledgeSearch(
        space, actor_id=actor_id, run_id=run_id, retry_unknown=retry_unknown
    )
    if checkpoint is not None:
        saved = checkpoint.get("modules", {}).get("tools", [])
        binding = next((item for item in saved if item.get("name") == "knowledge_search"), None)
        if binding is None or not binding.get("resume_inflight", False):

            async def legacy(libraries, query):
                return await adapter.search(
                    space,
                    libraries,
                    query,
                    actor_id=actor_id,
                    run_id=run_id,
                    retry_unknown=retry_unknown,
                )

            return legacy
    return adapter


class RunKnowledgeSearch:
    """Keep one Embedding operation per KB per logical tool invocation.

    ``invocation.set`` persists before any external request. A new tool call gets
    a fresh invocation state even when its query text is identical. Resuming the
    same call keeps successful earlier KBs, failed later KBs and Hook progress.
    """

    def __init__(self, space, *, actor_id, run_id, retry_unknown=False, search=None, timeout=80):
        if type(retry_unknown) is not bool:
            raise ValueError("retry_unknown must be an explicit boolean")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Knowledge search timeout must be a positive finite number")
        self.space = space
        self.actor_id = actor_id
        self.run_id = run_id
        self.retry_unknown = retry_unknown
        self.search = search or knowledge.search
        self.timeout = timeout

    async def search_with_context(self, libraries, query, invocation):
        identity = {
            "query": query,
            "libraries": [
                {
                    "id": library["id"],
                    "embedding_id": library.get("embedding_id", ""),
                    "index_signature": (library.get("embedding_index") or {}).get("signature"),
                    "documents": deepcopy(library.get("documents")),
                }
                for library in libraries
            ],
        }
        embedding_ids = {library["id"] for library in libraries if library.get("embedding_id")}
        state = invocation.get(_STATE_KEY)
        if state is None:
            state = {
                "format": 1,
                "identity": identity,
                "operations": {kb_id: uuid4().hex for kb_id in sorted(embedding_ids)},
            }
            invocation.set(_STATE_KEY, state)
        elif (
            type(state) is not dict
            or state.get("format") != 1
            or state.get("identity") != identity
            or type(state.get("operations")) is not dict
            or set(state["operations"]) != embedding_ids
            or any(type(value) is not str or not value for value in state["operations"].values())
            or len(set(state["operations"].values())) != len(embedding_ids)
        ):
            raise HookRecoveryRequired("知识检索的持久化调用身份不一致，不能重新分配向量化操作")
        try:
            async with asyncio.timeout(self.timeout):
                return await self.search(
                    self.space,
                    libraries,
                    query,
                    actor_id=self.actor_id,
                    run_id=self.run_id,
                    embedding_operation_ids=deepcopy(state["operations"]),
                    retry_unknown=self.retry_unknown,
                )
        except EmbeddingOperationError as exc:
            # Keep the owning tool pending. This is an operation recovery gate,
            # not a database error or a tool failure for the LLM to work around.
            raise HookRecoveryRequired(
                f"知识检索向量化操作 {exc.operation_id} 尚未完成：{exc}"
            ) from None
        except TimeoutError:
            raise HookRecoveryRequired(
                "知识检索达到执行时限，已保留向量化操作，可恢复原调用"
            ) from None
