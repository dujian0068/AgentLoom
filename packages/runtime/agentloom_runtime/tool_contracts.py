"""Contracts shared by the loop, tool catalog and independently registered handlers."""

import asyncio
import copy
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Protocol

TOOL_REQUEST_TOPIC = "tool.execute"


class ToolPersistenceError(RuntimeError):
    """A required checkpoint failed; stop without consuming the current call."""


def readonly(value):
    """Detach configuration from its owner and recursively prohibit mutation."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(readonly(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(readonly(item) for item in value)
    return copy.deepcopy(value)


def safe_observer(emit):
    """Notifications cannot change outcomes; never log their payload or error text."""

    def observe(kind, payload):
        try:
            emit(kind, copy.deepcopy(payload))
        except (Exception, asyncio.CancelledError):
            logging.getLogger(__name__).warning("Tool observation delivery failed")

    return observe


class PlanEditor(Protocol):
    def update(self, steps: list[dict], explanation: str) -> dict: ...

    def read(self) -> tuple: ...


class ChildRunner(Protocol):
    async def run(self, subagent_id: str, task: str) -> dict: ...


class CitationRecorder(Protocol):
    def record(self, rows: list[dict]) -> list[dict]: ...


class InvocationState(Protocol):
    def get(self, key: str, default=None): ...

    def set(self, key: str, value) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Invocation capabilities without direct access to mutable loop state."""

    config: Mapping
    instance: str
    root_workspace: Path
    emit: Callable[[str, dict], None]
    plan: PlanEditor
    children: ChildRunner
    citations: CitationRecorder
    invocation: InvocationState
    authorize_tool: Callable[["ToolDefinition"], None] = lambda definition: None
    # Separate durable namespace for the runtime's operation journal.
    runtime_state: InvocationState | None = None

    def __post_init__(self):
        object.__setattr__(self, "config", readonly(self.config))
        object.__setattr__(self, "emit", safe_observer(self.emit))

    @property
    def workspace(self):
        if self.instance == "main":
            return self.root_workspace
        return self.root_workspace / "subagents" / self.instance


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    name: str
    arguments: str
    uncertain: bool = False


@dataclass(frozen=True)
class ToolOutcome:
    value: Any
    status: str
    evidence_arguments: dict = field(default_factory=dict)
    raw_value: Any = None
    has_raw_value: bool = False


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], Awaitable[Any]]
    available: Callable[[dict, str], bool] = lambda config, instance: True
    before_plan: bool = False
    resume_inflight: bool = False
    timeout: float = 90
    evidence_fields: tuple[str, ...] = ()
    # Version the implementation and its availability/policy behavior, not just its schema.
    implementation_id: str | None = None
    # Stable capability identity, independent from model-facing function aliases.
    tool_id: str | None = None

    def model_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": copy.deepcopy(self.parameters),
            },
        }


def parameters(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def bindings(snapshot, config, kind):
    return [resource for resource in snapshot[kind] if resource["id"] in config[kind]]
