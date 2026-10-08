"""Versioned, trusted Python Hooks for Runtime capability boundaries.

Uploaded or Git-sourced code must use a separate isolated worker; this SDK does
not import untrusted code into the application process.
"""

from .contracts import (
    Continue,
    HookBinding,
    HookContext,
    HookDefinition,
    HookError,
    HookFailed,
    HookPoint,
    HookRecoveryRequired,
    HookRejected,
    PatchInput,
    PatchOutput,
    Reject,
)
from .manager import HookManager
from .operation import execute_operation
from .registry import HookPointRegistry, HookRegistry

__all__ = [
    "Continue",
    "HookBinding",
    "HookContext",
    "HookDefinition",
    "HookError",
    "HookFailed",
    "HookManager",
    "HookPoint",
    "HookPointRegistry",
    "HookRecoveryRequired",
    "HookRegistry",
    "HookRejected",
    "PatchInput",
    "PatchOutput",
    "Reject",
    "execute_operation",
]
