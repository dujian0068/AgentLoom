"""Runtime ports; implementations never receive the Engine itself."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ModelRequest:
    messages: list[dict]
    tools: list[dict]
    purpose: str = "action"
    instance: str = "main"


class ModelGateway(Protocol):
    module_id: str

    async def invoke(self, request: ModelRequest) -> dict: ...


ModelCall = Callable[[ModelRequest], Awaitable[dict]]


@dataclass(frozen=True)
class ContextInput:
    task: str
    plan: list[dict]
    messages: list[dict]
    instance: str


@dataclass(frozen=True)
class CompactionResult:
    messages: list[dict]
    metrics: dict = field(default_factory=dict)


class CompactionPolicy(Protocol):
    module_id: str

    async def compact(self, context: ContextInput, model: ModelCall) -> CompactionResult | None: ...


@dataclass(frozen=True)
class CompletionInput:
    task: str
    plan: list[dict]
    evidence: list[dict]
    messages: list[dict]
    candidate: str
    instance: str


@dataclass(frozen=True)
class CompletionDecision:
    decision: str
    reason: str
    next_action: str = ""

    def __post_init__(self):
        if self.decision not in ("complete", "continue", "blocked"):
            raise ValueError("未知完成检查结果")
        if not isinstance(self.reason, str) or not isinstance(self.next_action, str):
            raise ValueError("完成检查需要文本原因和下一步")


class CompletionPolicy(Protocol):
    module_id: str

    async def review(self, context: CompletionInput, model: ModelCall) -> CompletionDecision: ...


class ExecutionStrategy(Protocol):
    module_id: str

    def instructions(self, config: dict, instance: str) -> str: ...

    def candidate_feedback(self, plan: list[dict], instance: str) -> str | None: ...

    def authorize_tool(self, definition, plan: list[dict], instance: str) -> None: ...


@dataclass(frozen=True)
class ExecutionLimits:
    max_model_calls: int = 96
    max_iterations: int = 64

    def __post_init__(self):
        for value in (self.max_model_calls, self.max_iterations):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("运行预算必须为正整数")


def normalize_model_response(message):
    """All gateways satisfy host invariants before messages/pending are committed."""
    try:
        if not isinstance(message, dict):
            raise ValueError()
        content, calls = message.get("content"), message.get("tool_calls")
        if content is not None and not isinstance(content, str):
            raise ValueError()
        if not calls and not (content or "").strip():
            raise ValueError()
        if calls:
            if not isinstance(calls, list):
                raise ValueError()
            ids = []
            for call in calls:
                if not isinstance(call["id"], str) or not call["id"]:
                    raise ValueError()
                if call.get("type", "function") != "function":
                    raise ValueError()
                if not isinstance(call["function"]["name"], str) or not call["function"]["name"]:
                    raise ValueError()
                if not isinstance(call["function"]["arguments"], str):
                    raise ValueError()
                ids.append(call["id"])
            if len(set(ids)) != len(ids):
                raise ValueError()
        reasoning = message.get("reasoning_content")
        if reasoning is not None and not isinstance(reasoning, str):
            raise ValueError()
        result = {
            key: deepcopy(value)
            for key, value in message.items()
            if key in ("content", "tool_calls", "reasoning_content")
        }
        result["role"] = "assistant"
        return result
    except (TypeError, KeyError, ValueError):
        raise ValueError("模型模块返回的消息或工具调用协议不合法") from None
