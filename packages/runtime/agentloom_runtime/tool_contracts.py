"""Contracts shared by the loop, tool catalog and independently registered handlers."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

TOOL_REQUEST_TOPIC = "tool.execute"


@dataclass
class ToolContext:
    """Trusted runtime state, supplied by the application rather than model arguments."""

    config: dict
    frame: dict
    state: dict
    pending: dict
    instance: str
    root_workspace: Path
    emit: Callable[[str, dict], None]
    persist: Callable[[], None]
    run_child: Callable[..., Awaitable[str]]

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

    def model_schema(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def parameters(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


def bindings(snapshot, config, kind):
    return [resource for resource in snapshot[kind] if resource["id"] in config[kind]]
