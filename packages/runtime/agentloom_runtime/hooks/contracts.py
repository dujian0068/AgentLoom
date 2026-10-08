"""The trusted Hook SDK. No runtime objects or execution callbacks enter a Hook."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


class HookError(RuntimeError):
    """A Hook boundary could not safely finish; the owning operation must stop."""


class HookFailed(HookError):
    """A required Hook failed, timed out, or returned an invalid decision."""


class HookRecoveryRequired(HookError):
    """An interrupted action may have side effects and cannot be blindly repeated."""


class HookRejected(HookError):
    def __init__(self, code: str, reason: str, *, binding_id: str = ""):
        self.code = code
        self.reason = reason
        self.binding_id = binding_id
        super().__init__(f"Hook rejected operation ({code}): {reason}")


@dataclass(frozen=True, slots=True)
class Continue:
    pass


@dataclass(frozen=True, slots=True)
class PatchInput:
    patch: dict


@dataclass(frozen=True, slots=True)
class PatchOutput:
    patch: dict


@dataclass(frozen=True, slots=True)
class Reject:
    code: str
    reason: str


HookResult = Continue | PatchInput | PatchOutput | Reject


@dataclass(frozen=True, slots=True)
class HookContext:
    point: str
    binding_id: str
    config: Mapping
    scope: Mapping
    deadline: float

    def __getattr__(self, name):
        if name in SCOPE_FIELDS:
            return self.scope.get(name)
        raise AttributeError(name)


SCOPE_FIELDS = frozenset(
    {
        "space_id",
        "actor_id",
        "run_id",
        "published_revision",
        "instance_id",
        "operation_id",
        "parent_operation_id",
        "call_id",
        "purpose",
        "target",
        "tool_id",
        "model_id",
        "kb_id",
        "index_revision",
        "index_signature",
    }
)


@dataclass(frozen=True)
class HookDefinition:
    hook_id: str
    version: str
    handler: Callable[[HookContext, Mapping], Awaitable[HookResult]]
    config_schema: dict = field(default_factory=lambda: {"type": "object"})
    replay_safe: bool = False
    observation: bool = False
    code_hash: str | None = None


@dataclass(frozen=True)
class HookBinding:
    binding_id: str
    hook_id: str
    point: str
    version: str | None = None
    priority: int = 100
    timeout: float = 1.0
    failure_policy: str = "block"
    config: dict = field(default_factory=dict)
    purposes: tuple[str, ...] | None = None
    instances: tuple[str, ...] = ("main", "child")
    targets: tuple[str, ...] = ()


@dataclass(frozen=True)
class HookPoint:
    name: str
    phase: str
    writable_fields: tuple[str, ...] = ()
    allow_reject: bool = False
    observation_only: bool = False
    schema: dict = field(default_factory=lambda: {"type": "object"})


Validator = Callable[[dict], Any]
